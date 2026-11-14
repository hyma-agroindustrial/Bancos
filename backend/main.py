import os
import json
import hashlib
import secrets
import calendar
from datetime import datetime, date
from decimal import Decimal
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, and_
from typing import List, Optional, Dict
from datetime import date
from decimal import Decimal

from .database import engine, Base, get_db
from .models import Cuenta, Categoria, Entidad, Transaccion, Usuario, Configuracion, EstadoCuentaArchivo
from .schemas import (
    CuentaCreate, CuentaUpdate, CuentaResponse,
    CategoriaCreate, CategoriaUpdate, CategoriaResponse,
    EntidadCreate, EntidadUpdate, EntidadResponse,
    TransaccionCreate, TransaccionUpdate, TransaccionResponse,
    MovimientoExtraido, BatchImportConfirm,
    UsuarioLogin, UsuarioCreate, UsuarioUpdate, UsuarioResponse, AuthResponse,
    ConfiguracionData, EstadoCuentaArchivoResponse
)
from .parsers.bank_pdf_parser import extract_movements_from_pdf

# Inicializar Base de Datos
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Consolidado de Bancos - Control Fiscal & Gastos",
    version="1.2.0",
    description="Sistema contable automatizado con personalización de marca, captura al vuelo de categorías y conciliación bancaria."
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def hash_pin(pin: str) -> str:
    return hashlib.sha256(f"hyma_bancos_salt_{pin}".encode("utf-8")).hexdigest()

# SEED DATA AL INICIO
@app.on_event("startup")
def startup_seed_data():
    db = next(get_db())
    try:
        # 1. Seed Configuración del Encabezado (Nombre, Descripción, Logo)
        cfg_defaults = {
            "nombre_sistema": "Consolidado de Bancos",
            "descripcion_sistema": "Sistema de Control de Gastos & Cuentas Fiscales",
            "logo_url": ""
        }
        for k, v in cfg_defaults.items():
            if not db.query(Configuracion).filter(Configuracion.clave == k).first():
                db.add(Configuracion(clave=k, valor=v))
        db.commit()

        # 2. Seed Usuario Administrador por defecto
        if db.query(Usuario).count() == 0:
            admin_user = Usuario(
                username="admin",
                nombre="Administrador General",
                pin_hash=hash_pin("1234"),
                rol="ADMIN",
                activo=True
            )
            db.add(admin_user)
            db.commit()

        # 3. Seed Categorías iniciales y ampliadas
        cats_to_seed = [
            ("Depósito Cliente", "ENTRADA", "CLIENTE", "Cobranza de cosechas y productos agrícolas"),
            ("Pago Gasolina / Diésel", "SALIDA", "GASOLINERA", "Combustible de tractores, maquinaria y vehículos"),
            ("Insumos Agrícolas", "SALIDA", "PROVEEDOR", "Semillas, fertilizantes, foliares y agroquímicos"),
            ("Herramienta y Taller", "SALIDA", "PROVEEDOR", "Herramientas de trabajo, equipo y mantenimiento"),
            ("Papelería y Oficina", "SALIDA", "PROVEEDOR", "Artículos de papelería, impresiones y oficina"),
            ("Comida y Viáticos", "SALIDA", "GENERAL", "Alimentos, viáticos de choferes y comidas de campo"),
            ("Accesorios y Refacciones", "SALIDA", "PROVEEDOR", "Refacciones de maquinaria pesada, implementos"),
            ("Ferretería y Materiales", "SALIDA", "PROVEEDOR", "Materiales de construcción, tubería, mallas"),
            ("Luz (CFE)", "SALIDA", "PROVEEDOR", "Recibos de energía eléctrica de pozos, bodegas y oficina"),
            ("Teléfonos e Internet", "SALIDA", "PROVEEDOR", "Planes celulares, radiofrecuencia e internet en rancho"),
            ("Agua y Derechos de Riego", "SALIDA", "PROVEEDOR", "Módulos de riego, títulos Conagua, cuotas de agua"),
            ("Rentas de Tierras / Bodegas", "SALIDA", "PROVEEDOR", "Arrendamiento de parcelas, bodegas y locales"),
            ("Pago Nómina / Jornales", "SALIDA", "EMPLEADO", "Pago a jornaleros de campo y personal de nómina"),
            ("Impuestos SAT / IMSS", "SALIDA", "SAT", "Pagos provisionales, IVA, retenciones y cuotas obrero-patronales"),
            ("Traspaso Entre Cuentas", "AMBOS", "GENERAL", "Movimientos internos entre cuentas de la misma empresa"),
        ]
        
        for nom, tipo, tip_ent, desc in cats_to_seed:
            if not db.query(Categoria).filter(Categoria.nombre == nom).first():
                db.add(Categoria(nombre=nom, tipo=tipo, tipo_entidad_sugerida=tip_ent, descripcion=desc))
        db.commit()

        # 4. Seed cuenta demo si no existe ninguna
        if db.query(Cuenta).count() == 0:
            demo_acc = Cuenta(
                nombre="Fiscal Principal",
                banco="BBVA México",
                clabe="012180001234567890",
                numero_cuenta="0123456789",
                saldo_inicial=Decimal("50000.00"),
                moneda="MXN"
            )
            db.add(demo_acc)
            db.commit()

    finally:
        db.close()


# ====================================================================================================
# CONFIGURACIÓN PERSONALIZABLE DE MARCA (NOMBRE, LOGO, DESCRIPCIÓN)
# ====================================================================================================
@app.get("/api/configuracion", response_model=ConfiguracionData)
def obtener_configuracion(db: Session = Depends(get_db)):
    cfg_rows = db.query(Configuracion).all()
    cfg_dict = {r.clave: r.valor for r in cfg_rows}
    return ConfiguracionData(
        nombre_sistema=cfg_dict.get("nombre_sistema", "Consolidado de Bancos"),
        descripcion_sistema=cfg_dict.get("descripcion_sistema", "Sistema de Control de Gastos & Cuentas Fiscales"),
        logo_url=cfg_dict.get("logo_url", ""),
        tema_color=cfg_dict.get("tema_color", "perla")
    )

@app.post("/api/configuracion", response_model=ConfiguracionData)
def guardar_configuracion(data: ConfiguracionData, db: Session = Depends(get_db)):
    for k, v in [
        ("nombre_sistema", data.nombre_sistema),
        ("descripcion_sistema", data.descripcion_sistema),
        ("logo_url", data.logo_url or ""),
        ("tema_color", data.tema_color or "perla")
    ]:
        row = db.query(Configuracion).filter(Configuracion.clave == k).first()
        if row:
            row.valor = v
        else:
            db.add(Configuracion(clave=k, valor=v))
    db.commit()
    return data

@app.post("/api/configuracion/upload-logo", response_model=ConfiguracionData)
async def subir_fotografia_logo(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Permite subir una imagen o fotografía directamente desde la computadora o celular."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Archivo no proporcionado.")
    
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in [".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"]:
        raise HTTPException(status_code=400, detail="Formato de imagen no soportado. Sube un archivo PNG, JPG o WEBP.")

    uploads_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "static", "uploads")
    os.makedirs(uploads_dir, exist_ok=True)

    clean_filename = f"logo_{secrets.token_hex(4)}{ext}"
    file_path = os.path.join(uploads_dir, clean_filename)

    content = await file.read()
    with open(file_path, "wb") as f:
        f.write(content)

    logo_url = f"/static/uploads/{clean_filename}"

    row = db.query(Configuracion).filter(Configuracion.clave == "logo_url").first()
    if row:
        row.valor = logo_url
    else:
        db.add(Configuracion(clave="logo_url", valor=logo_url))
    db.commit()

    cfg_rows = db.query(Configuracion).all()
    cfg_dict = {r.clave: r.valor for r in cfg_rows}
    return ConfiguracionData(
        nombre_sistema=cfg_dict.get("nombre_sistema", "Consolidado de Bancos"),
        descripcion_sistema=cfg_dict.get("descripcion_sistema", "Sistema de Control de Gastos & Cuentas Fiscales"),
        logo_url=logo_url,
        tema_color=cfg_dict.get("tema_color", "perla")
    )


# ====================================================================================================
# 0. CONTROL DE ACCESO (USUARIO + PIN) PARA ACCESO DESDE CUALQUIER DISPOSITIVO
# ====================================================================================================
@app.post("/api/auth/login", response_model=AuthResponse)
def login_usuario(payload: UsuarioLogin, db: Session = Depends(get_db)):
    user = db.query(Usuario).filter(Usuario.username == payload.username.strip(), Usuario.activo == True).first()
    if not user or user.pin_hash != hash_pin(payload.pin.strip()):
        raise HTTPException(status_code=401, detail="Usuario o PIN incorrecto.")

    token = secrets.token_hex(24)
    user.token_sesion = token
    db.commit()

    return {
        "token": token,
        "usuario": UsuarioResponse.model_validate(user)
    }

@app.get("/api/auth/verify")
def verificar_sesion(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    if not authorization:
        raise HTTPException(status_code=401, detail="No autorizado.")
    token = authorization.replace("Bearer ", "").strip()
    user = db.query(Usuario).filter(Usuario.token_sesion == token, Usuario.activo == True).first()
    if not user:
        raise HTTPException(status_code=401, detail="Sesión inválida o expirada.")
    return UsuarioResponse.model_validate(user)

@app.get("/api/usuarios", response_model=List[UsuarioResponse])
def listar_usuarios(db: Session = Depends(get_db)):
    return db.query(Usuario).filter(Usuario.activo == True).order_by(Usuario.id).all()

@app.post("/api/usuarios", response_model=UsuarioResponse)
def crear_usuario(payload: UsuarioCreate, db: Session = Depends(get_db)):
    existente = db.query(Usuario).filter(Usuario.username == payload.username.strip()).first()
    if existente:
        raise HTTPException(status_code=400, detail="Este nombre de usuario ya existe.")

    nuevo = Usuario(
        username=payload.username.strip(),
        nombre=payload.nombre.strip(),
        pin_hash=hash_pin(payload.pin.strip()),
        rol=payload.rol or "OPERADOR",
        cuentas_permitidas=json.dumps(payload.cuentas_permitidas or []),
        activo=True
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    return nuevo

@app.put("/api/usuarios/{usuario_id}", response_model=UsuarioResponse)
def actualizar_usuario(usuario_id: int, payload: UsuarioUpdate, db: Session = Depends(get_db)):
    user = db.query(Usuario).filter(Usuario.id == usuario_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")

    if payload.nombre is not None:
        user.nombre = payload.nombre.strip()
    if payload.pin is not None and payload.pin.strip():
        user.pin_hash = hash_pin(payload.pin.strip())
    if payload.rol is not None:
        if user.username == "admin" and payload.rol != "ADMIN":
            raise HTTPException(status_code=400, detail="El administrador principal siempre debe mantener rol ADMIN.")
        user.rol = payload.rol
    if payload.activo is not None:
        user.activo = payload.activo
    if payload.cuentas_permitidas is not None:
        user.cuentas_permitidas = json.dumps(payload.cuentas_permitidas)

    db.commit()
    db.refresh(user)
    return user

@app.delete("/api/usuarios/{usuario_id}")
def eliminar_usuario(usuario_id: int, db: Session = Depends(get_db)):
    user = db.query(Usuario).filter(Usuario.id == usuario_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")
    if user.username == "admin":
        raise HTTPException(status_code=400, detail="No se puede eliminar el usuario administrador principal.")
    user.activo = False
    db.commit()
    return {"message": "Usuario desactivado con éxito."}


# ====================================================================================================
# 1. GESTIÓN DE CUENTAS BANCARIAS (MODIFICACIÓN Y ELIMINACIÓN)
# ====================================================================================================
@app.get("/api/cuentas", response_model=List[CuentaResponse])
def listar_cuentas(db: Session = Depends(get_db)):
    cuentas = db.query(Cuenta).filter(Cuenta.activo == True).all()
    resultado = []
    
    for c in cuentas:
        entradas = db.query(func.coalesce(func.sum(Transaccion.monto), 0)).filter(
            Transaccion.cuenta_id == c.id, Transaccion.tipo == "ENTRADA"
        ).scalar()
        
        salidas = db.query(func.coalesce(func.sum(Transaccion.monto), 0)).filter(
            Transaccion.cuenta_id == c.id, Transaccion.tipo == "SALIDA"
        ).scalar()

        saldo_actual = Decimal(str(c.saldo_inicial)) + Decimal(str(entradas)) - Decimal(str(salidas))
        
        c_dict = CuentaResponse.model_validate(c)
        c_dict.total_entradas = Decimal(str(entradas))
        c_dict.total_salidas = Decimal(str(salidas))
        c_dict.saldo_actual = saldo_actual
        resultado.append(c_dict)

    return resultado

@app.post("/api/cuentas", response_model=CuentaResponse)
def crear_cuenta(cuenta: CuentaCreate, db: Session = Depends(get_db)):
    existente = db.query(Cuenta).filter(
        (Cuenta.clabe == cuenta.clabe) | (Cuenta.numero_cuenta == cuenta.numero_cuenta)
    ).first()
    if existente:
        raise HTTPException(status_code=400, detail="Ya existe una cuenta con la misma CLABE o Número de cuenta.")

    nueva_cuenta = Cuenta(**cuenta.model_dump())
    db.add(nueva_cuenta)
    db.commit()
    db.refresh(nueva_cuenta)
    
    c_res = CuentaResponse.model_validate(nueva_cuenta)
    c_res.saldo_actual = nueva_cuenta.saldo_inicial
    return c_res

@app.put("/api/cuentas/{cuenta_id}", response_model=CuentaResponse)
def modificar_cuenta(cuenta_id: int, datos: CuentaUpdate, db: Session = Depends(get_db)):
    cuenta = db.query(Cuenta).filter(Cuenta.id == cuenta_id).first()
    if not cuenta:
        raise HTTPException(status_code=404, detail="Cuenta bancaria no encontrada.")

    if datos.clabe and datos.clabe != cuenta.clabe:
        existe_clabe = db.query(Cuenta).filter(Cuenta.clabe == datos.clabe, Cuenta.id != cuenta_id).first()
        if existe_clabe:
            raise HTTPException(status_code=400, detail="La nueva CLABE ya pertenece a otra cuenta.")
        cuenta.clabe = datos.clabe

    if datos.numero_cuenta and datos.numero_cuenta != cuenta.numero_cuenta:
        existe_num = db.query(Cuenta).filter(Cuenta.numero_cuenta == datos.numero_cuenta, Cuenta.id != cuenta_id).first()
        if existe_num:
            raise HTTPException(status_code=400, detail="El nuevo número de cuenta ya pertenece a otra cuenta.")
        cuenta.numero_cuenta = datos.numero_cuenta

    if datos.nombre is not None:
        cuenta.nombre = datos.nombre.strip()
    if datos.rfc_empresa is not None:
        cuenta.rfc_empresa = datos.rfc_empresa.strip() if datos.rfc_empresa else None
    if datos.logo_empresa is not None:
        cuenta.logo_empresa = datos.logo_empresa
    if datos.banco is not None:
        cuenta.banco = datos.banco.strip()
    if datos.logo_banco is not None:
        cuenta.logo_banco = datos.logo_banco
    if datos.saldo_inicial is not None:
        cuenta.saldo_inicial = datos.saldo_inicial
    if datos.moneda is not None:
        cuenta.moneda = datos.moneda
    if datos.activo is not None:
        cuenta.activo = datos.activo

    db.commit()
    db.refresh(cuenta)

    entradas = db.query(func.coalesce(func.sum(Transaccion.monto), 0)).filter(
        Transaccion.cuenta_id == cuenta.id, Transaccion.tipo == "ENTRADA"
    ).scalar()
    salidas = db.query(func.coalesce(func.sum(Transaccion.monto), 0)).filter(
        Transaccion.cuenta_id == cuenta.id, Transaccion.tipo == "SALIDA"
    ).scalar()
    
    c_res = CuentaResponse.model_validate(cuenta)
    c_res.total_entradas = Decimal(str(entradas))
    c_res.total_salidas = Decimal(str(salidas))
    c_res.saldo_actual = Decimal(str(cuenta.saldo_inicial)) + c_res.total_entradas - c_res.total_salidas
    return c_res

@app.delete("/api/cuentas/{cuenta_id}")
def eliminar_cuenta(cuenta_id: int, db: Session = Depends(get_db)):
    cuenta = db.query(Cuenta).filter(Cuenta.id == cuenta_id).first()
    if not cuenta:
        raise HTTPException(status_code=404, detail="Cuenta no encontrada.")

    cuenta.activo = False
    db.commit()
    return {"message": "Cuenta bancaria eliminada con éxito."}


# ====================================================================================================
# 2. CATEGORÍAS (CREACIÓN AL VUELO) & ENTIDADES
# ====================================================================================================
@app.get("/api/categorias", response_model=List[CategoriaResponse])
def listar_categorias(tipo: Optional[str] = None, db: Session = Depends(get_db)):
    query = db.query(Categoria).filter(Categoria.activo == True)
    if tipo:
        query = query.filter((Categoria.tipo == tipo) | (Categoria.tipo == "AMBOS"))
    return query.order_by(Categoria.nombre).all()

@app.post("/api/categorias", response_model=CategoriaResponse)
def crear_categoria(cat: CategoriaCreate, db: Session = Depends(get_db)):
    """Permite crear categorías al vuelo (Papelería, Herramientas, Comida, etc.)."""
    nombre_limpio = cat.nombre.strip()
    existente = db.query(Categoria).filter(func.lower(Categoria.nombre) == func.lower(nombre_limpio)).first()
    if existente:
        return existente
        
    nueva = Categoria(
        nombre=nombre_limpio,
        tipo=cat.tipo,
        tipo_entidad_sugerida=cat.tipo_entidad_sugerida or "GENERAL",
        descripcion=cat.descripcion,
        activo=True
    )
    db.add(nueva)
    db.commit()
    db.refresh(nueva)
    return nueva

@app.put("/api/categorias/{categoria_id}", response_model=CategoriaResponse)
def modificar_categoria(categoria_id: int, datos: CategoriaUpdate, db: Session = Depends(get_db)):
    """Permite corregir el nombre u opciones de una categoría existente."""
    cat = db.query(Categoria).filter(Categoria.id == categoria_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Categoría no encontrada.")

    if datos.nombre is not None:
        nombre_limpio = datos.nombre.strip()
        duplicado = db.query(Categoria).filter(
            func.lower(Categoria.nombre) == func.lower(nombre_limpio),
            Categoria.id != categoria_id
        ).first()
        if duplicado:
            raise HTTPException(status_code=400, detail=f"Ya existe otra categoría con el nombre '{nombre_limpio}'.")
        cat.nombre = nombre_limpio

    if datos.tipo is not None:
        cat.tipo = datos.tipo
    if datos.tipo_entidad_sugerida is not None:
        cat.tipo_entidad_sugerida = datos.tipo_entidad_sugerida
    if datos.descripcion is not None:
        cat.descripcion = datos.descripcion

    db.commit()
    db.refresh(cat)
    return cat

@app.delete("/api/categorias/{categoria_id}")
def eliminar_categoria(categoria_id: int, db: Session = Depends(get_db)):
    """Elimina una categoría desvinculándola de movimientos existentes."""
    cat = db.query(Categoria).filter(Categoria.id == categoria_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Categoría no encontrada.")

    db.query(Transaccion).filter(Transaccion.categoria_id == categoria_id).update({Transaccion.categoria_id: None})
    db.delete(cat)
    db.commit()
    return {"message": f"Categoría '{cat.nombre}' eliminada con éxito."}

@app.get("/api/entidades", response_model=List[EntidadResponse])
def listar_entidades(tipo_entidad: Optional[str] = None, db: Session = Depends(get_db)):
    query = db.query(Entidad).filter(Entidad.activo == True)
    if tipo_entidad and tipo_entidad != "GENERAL":
        query = query.filter(Entidad.tipo_entidad == tipo_entidad)
    return query.order_by(Entidad.nombre).all()

@app.post("/api/entidades", response_model=EntidadResponse)
def crear_entidad_al_vuelo(entidad: EntidadCreate, db: Session = Depends(get_db)):
    nombre_limpio = entidad.nombre.strip()
    existente = db.query(Entidad).filter(
        func.lower(Entidad.nombre) == func.lower(nombre_limpio),
        Entidad.tipo_entidad == entidad.tipo_entidad
    ).first()
    if existente:
        return existente

    nueva = Entidad(
        nombre=nombre_limpio,
        tipo_entidad=entidad.tipo_entidad,
        rfc=entidad.rfc.strip().upper() if entidad.rfc else None,
        telefono=entidad.telefono,
        email=entidad.email,
        activo=True
    )
    db.add(nueva)
    db.commit()
    db.refresh(nueva)
    return nueva

@app.put("/api/entidades/{entidad_id}", response_model=EntidadResponse)
def modificar_entidad(entidad_id: int, datos: EntidadUpdate, db: Session = Depends(get_db)):
    """Permite corregir el nombre, tipo o RFC de un cliente o proveedor."""
    ent = db.query(Entidad).filter(Entidad.id == entidad_id).first()
    if not ent:
        raise HTTPException(status_code=404, detail="Entidad no encontrada.")

    if datos.nombre is not None:
        nombre_limpio = datos.nombre.strip()
        tipo_final = datos.tipo_entidad if datos.tipo_entidad is not None else ent.tipo_entidad
        duplicado = db.query(Entidad).filter(
            func.lower(Entidad.nombre) == func.lower(nombre_limpio),
            Entidad.tipo_entidad == tipo_final,
            Entidad.id != entidad_id
        ).first()
        if duplicado:
            raise HTTPException(status_code=400, detail=f"Ya existe un registro con el nombre '{nombre_limpio}' para este tipo.")
        ent.nombre = nombre_limpio

    if datos.tipo_entidad is not None:
        ent.tipo_entidad = datos.tipo_entidad
    if datos.rfc is not None:
        ent.rfc = datos.rfc.strip().upper() if datos.rfc else None
    if datos.telefono is not None:
        ent.telefono = datos.telefono.strip() if datos.telefono else None
    if datos.email is not None:
        ent.email = datos.email.strip() if datos.email else None

    db.commit()
    db.refresh(ent)
    return ent

@app.delete("/api/entidades/{entidad_id}")
def eliminar_entidad(entidad_id: int, db: Session = Depends(get_db)):
    """Elimina un cliente/proveedor desvinculándolo de movimientos existentes."""
    ent = db.query(Entidad).filter(Entidad.id == entidad_id).first()
    if not ent:
        raise HTTPException(status_code=404, detail="Entidad no encontrada.")

    db.query(Transaccion).filter(Transaccion.entidad_id == entidad_id).update({Transaccion.entidad_id: None})
    db.delete(ent)
    db.commit()
    return {"message": f"Entidad '{ent.nombre}' eliminada con éxito."}


# ====================================================================================================
# 3. CAPTURA Y GESTIÓN DE TRANSACCIONES
# ====================================================================================================
@app.get("/api/transacciones", response_model=List[TransaccionResponse])
def listar_transacciones(
    cuenta_id: Optional[int] = None,
    categoria_id: Optional[int] = None,
    entidad_id: Optional[int] = None,
    tipo: Optional[str] = None,
    fecha_inicio: Optional[date] = None,
    fecha_fin: Optional[date] = None,
    limit: int = 150,
    db: Session = Depends(get_db)
):
    query = db.query(Transaccion)
    
    if cuenta_id:
        query = query.filter(Transaccion.cuenta_id == cuenta_id)
    if categoria_id:
        query = query.filter(Transaccion.categoria_id == categoria_id)
    if entidad_id:
        query = query.filter(Transaccion.entidad_id == entidad_id)
    if tipo:
        query = query.filter(Transaccion.tipo == tipo)
    if fecha_inicio:
        query = query.filter(Transaccion.fecha >= fecha_inicio)
    if fecha_fin:
        query = query.filter(Transaccion.fecha <= fecha_fin)

    transacciones = query.order_by(Transaccion.fecha.desc(), Transaccion.id.desc()).limit(limit).all()

    resultado = []
    for t in transacciones:
        tr_dict = TransaccionResponse.model_validate(t)
        tr_dict.cuenta_nombre = t.cuenta.nombre if t.cuenta else None
        tr_dict.cuenta_banco = t.cuenta.banco if t.cuenta else None
        tr_dict.cuenta_numero = t.cuenta.numero_cuenta if t.cuenta else None
        tr_dict.categoria_nombre = t.categoria.nombre if t.categoria else None
        tr_dict.entidad_nombre = t.entidad.nombre if t.entidad else None
        resultado.append(tr_dict)
        
    return resultado

@app.post("/api/transacciones", response_model=TransaccionResponse)
def crear_transaccion_manual(trans: TransaccionCreate, db: Session = Depends(get_db)):
    cuenta = db.query(Cuenta).filter(Cuenta.id == trans.cuenta_id).first()
    if not cuenta:
        raise HTTPException(status_code=404, detail="Cuenta bancaria no encontrada.")

    nueva = Transaccion(**trans.model_dump())
    db.add(nueva)
    db.commit()
    db.refresh(nueva)

    tr_dict = TransaccionResponse.model_validate(nueva)
    tr_dict.cuenta_nombre = cuenta.nombre
    tr_dict.cuenta_banco = cuenta.banco
    tr_dict.cuenta_numero = cuenta.numero_cuenta
    tr_dict.categoria_nombre = nueva.categoria.nombre if nueva.categoria else None
    tr_dict.entidad_nombre = nueva.entidad.nombre if nueva.entidad else None
    return tr_dict

@app.put("/api/transacciones/{transaccion_id}", response_model=TransaccionResponse)
def modificar_transaccion(transaccion_id: int, datos: TransaccionUpdate, db: Session = Depends(get_db)):
    """Permite modificar fecha, monto, concepto, categoría, cuenta o beneficiario de un movimiento existente."""
    t = db.query(Transaccion).filter(Transaccion.id == transaccion_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Movimiento no encontrado.")

    if datos.cuenta_id is not None:
        cta = db.query(Cuenta).filter(Cuenta.id == datos.cuenta_id).first()
        if not cta:
            raise HTTPException(status_code=404, detail="Cuenta bancaria no válida.")
        t.cuenta_id = datos.cuenta_id

    # Manejar actualización de categoría y entidad (incluyendo si se desasigna)
    if datos.categoria_id is not None:
        t.categoria_id = datos.categoria_id
    elif "categoria_id" in datos.model_fields_set:
        t.categoria_id = None

    if datos.entidad_id is not None:
        t.entidad_id = datos.entidad_id
    elif "entidad_id" in datos.model_fields_set:
        t.entidad_id = None

    if datos.fecha is not None:
        t.fecha = datos.fecha
    if datos.tipo is not None:
        t.tipo = datos.tipo
    if datos.monto is not None:
        t.monto = datos.monto
    if datos.concepto is not None:
        t.concepto = datos.concepto.strip()
    if datos.referencia is not None:
        t.referencia = datos.referencia.strip()

    db.commit()
    db.refresh(t)

    tr_dict = TransaccionResponse.model_validate(t)
    tr_dict.cuenta_nombre = t.cuenta.nombre if t.cuenta else None
    tr_dict.cuenta_banco = t.cuenta.banco if t.cuenta else None
    tr_dict.cuenta_numero = t.cuenta.numero_cuenta if t.cuenta else None
    tr_dict.categoria_nombre = t.categoria.nombre if t.categoria else None
    tr_dict.entidad_nombre = t.entidad.nombre if t.entidad else None
    return tr_dict

@app.delete("/api/transacciones/{transaccion_id}")
def eliminar_transaccion(transaccion_id: int, db: Session = Depends(get_db)):
    """Elimina permanentemente una transacción de la base de datos."""
    t = db.query(Transaccion).filter(Transaccion.id == transaccion_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Movimiento no encontrado.")
    db.delete(t)
    db.commit()
    return {"message": "Movimiento eliminado exitosamente."}


# ====================================================================================================
# 4. EXTRACCIÓN AUTOMATIZADA DE PDF BANCARIO (ESTADOS DE CUENTA)
# ====================================================================================================
@app.post("/api/transacciones/upload-pdf", response_model=List[MovimientoExtraido])
async def extraer_movimientos_pdf(
    cuenta_id: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    cuenta = db.query(Cuenta).filter(Cuenta.id == cuenta_id).first()
    if not cuenta:
        raise HTTPException(status_code=404, detail="Cuenta bancaria especificada no existe.")

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="El archivo debe ser un documento PDF válido.")

    content = await file.read()
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="El archivo PDF está vacío.")

    existing_hashes = set(
        h[0] for h in db.query(Transaccion.hash_deduplicacion).filter(
            Transaccion.cuenta_id == cuenta_id, Transaccion.hash_deduplicacion != None
        ).all()
    )

    movimientos = extract_movements_from_pdf(content, cuenta_id, existing_hashes)

    cats = db.query(Categoria).all()
    gas_cat = next((c.id for c in cats if "GASOLINA" in c.nombre.upper() or "DIESEL" in c.nombre.upper()), None)
    cli_cat = next((c.id for c in cats if "CLIENTE" in c.nombre.upper()), None)
    sat_cat = next((c.id for c in cats if "IMPUESTO" in c.nombre.upper() or "SAT" in c.nombre.upper()), None)
    cfe_cat = next((c.id for c in cats if "LUZ" in c.nombre.upper() or "CFE" in c.nombre.upper()), None)
    tel_cat = next((c.id for c in cats if "TEL" in c.nombre.upper() or "INTERNET" in c.nombre.upper()), None)
    com_cat = next((c.id for c in cats if "COMISION" in c.nombre.upper()), None)
    cred_cat = next((c.id for c in cats if "CREDITO" in c.nombre.upper() or "AUTOMOTRIZ" in c.nombre.upper()), None)
    hon_cat = next((c.id for c in cats if "HONORARIO" in c.nombre.upper()), None)
    trasp_cat = next((c.id for c in cats if "TRASPASO" in c.nombre.upper()), None)
    refac_cat = next((c.id for c in cats if "ACCESORIO" in c.nombre.upper() or "REFACCION" in c.nombre.upper()), None)

    for m in movimientos:
        conc = m["concepto"].upper()
        if any(w in conc for w in ["GM FINANCIAL", "CHEYENNE", "AUTOMOTRIZ", "ARRENDAMIENTO FINANCIERO"]):
            m["categoria_id_sugerida"] = cred_cat
        elif any(w in conc for w in ["HONORARIOS", "CONTABLES", "VALENZUELA EDEZA", "EDEZA"]):
            m["categoria_id_sugerida"] = hon_cat
        elif any(w in conc for w in ["GASOL", "COMBUSTIBLE", "HIDROCARB", "OXXO GAS", "PEMEX"]):
            m["categoria_id_sugerida"] = gas_cat
        elif any(w in conc for w in ["CFE", "COMISION FEDERAL", "LUZ"]):
            m["categoria_id_sugerida"] = cfe_cat
        elif any(w in conc for w in ["TELCEL", "TELMEX", "TOTALPLAY", "AT&T", "INTERNET"]):
            m["categoria_id_sugerida"] = tel_cat
        elif any(w in conc for w in ["SAT", "IMPUESTO", "TESORERIA", "IMSS"]):
            m["categoria_id_sugerida"] = sat_cat
        elif any(w in conc for w in ["COMISION", "MEMBRESIA", "ORDEN DE PAGO SPEI", "TRANSFERENCIA - ENVIO"]):
            m["categoria_id_sugerida"] = com_cat
        elif any(w in conc for w in ["TRASPASO", "SCOTIA", "SCOTIABANK"]):
            m["categoria_id_sugerida"] = trasp_cat
        elif any(w in conc for w in ["TOYOTA", "CAM TOYOTA", "REFACCION"]):
            m["categoria_id_sugerida"] = refac_cat
        elif m["tipo"] == "ENTRADA":
            m["categoria_id_sugerida"] = cli_cat

    return movimientos

@app.post("/api/transacciones/batch-import")
def confirmar_importacion_lote(payload: BatchImportConfirm, db: Session = Depends(get_db)):
    insertados = 0
    ignorados = 0

    for m in payload.movimientos:
        if m.es_duplicado:
            ignorados += 1
            continue

        existe = db.query(Transaccion).filter(Transaccion.hash_deduplicacion == m.hash_deduplicacion).first()
        if existe:
            ignorados += 1
            continue

        nueva_trans = Transaccion(
            cuenta_id=payload.cuenta_id,
            categoria_id=m.categoria_id_sugerida,
            entidad_id=m.entidad_id_sugerida,
            fecha=m.fecha,
            tipo=m.tipo,
            monto=m.monto,
            concepto=m.concepto,
            referencia=m.referencia,
            origen="PDF_EXTRACT",
            archivo_fuente=payload.archivo_nombre,
            hash_deduplicacion=m.hash_deduplicacion
        )
        db.add(nueva_trans)
        insertados += 1

    db.commit()
    return {"message": f"Proceso completado. {insertados} movimientos importados, {ignorados} omitidos por duplicidad."}


# ====================================================================================================
# 5. REPORTES, BALANCES Y AUDITORÍA
# ====================================================================================================
@app.get("/api/reportes/resumen")
def reporte_financiero_global(
    cuenta_id: Optional[int] = None,
    fecha_inicio: Optional[date] = None,
    fecha_fin: Optional[date] = None,
    db: Session = Depends(get_db)
):
    query = db.query(Transaccion)
    if cuenta_id:
        query = query.filter(Transaccion.cuenta_id == cuenta_id)
    if fecha_inicio:
        query = query.filter(Transaccion.fecha >= fecha_inicio)
    if fecha_fin:
        query = query.filter(Transaccion.fecha <= fecha_fin)

    total_entradas = query.filter(Transaccion.tipo == "ENTRADA").with_entities(func.coalesce(func.sum(Transaccion.monto), 0)).scalar()
    total_salidas = query.filter(Transaccion.tipo == "SALIDA").with_entities(func.coalesce(func.sum(Transaccion.monto), 0)).scalar()
    balance_periodo = Decimal(str(total_entradas)) - Decimal(str(total_salidas))

    return {
        "total_entradas": total_entradas,
        "total_salidas": total_salidas,
        "balance_periodo": balance_periodo
    }

NOMBRE_MESES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
    5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"
}

@app.get("/api/reportes/estado-cuenta")
def generar_estado_cuenta_mensual(
    cuenta_id: int,
    mes: int = Query(..., ge=1, le=12),
    anio: int = Query(..., ge=2000, le=2100),
    db: Session = Depends(get_db)
):
    """
    Genera un Estado de Cuenta Oficial / Corte Mensual al estilo bancario (Banorte, Scotiabank, BBVA)
    con Saldo Inicial acumulado al 1° del mes, saldo progresivo renglón por renglón,
    cuadro de balance cuadrado y desglose fiscal por categorías.
    """
    cuenta = db.query(Cuenta).filter(Cuenta.id == cuenta_id).first()
    if not cuenta:
        raise HTTPException(status_code=404, detail="Cuenta bancaria no encontrada.")

    # 1. Determinar rango exacto del mes
    last_day = calendar.monthrange(anio, mes)[1]
    fecha_inicio = date(anio, mes, 1)
    fecha_fin = date(anio, mes, last_day)

    # 2. Calcular Saldo Inicial Histórico al 1° del mes:
    # Saldo Inicial = Saldo Base Registrado + Sum(Entradas previas) - Sum(Salidas previas)
    entradas_previas = db.query(func.coalesce(func.sum(Transaccion.monto), 0)).filter(
        Transaccion.cuenta_id == cuenta_id,
        Transaccion.tipo == "ENTRADA",
        Transaccion.fecha < fecha_inicio
    ).scalar()

    salidas_previas = db.query(func.coalesce(func.sum(Transaccion.monto), 0)).filter(
        Transaccion.cuenta_id == cuenta_id,
        Transaccion.tipo == "SALIDA",
        Transaccion.fecha < fecha_inicio
    ).scalar()

    saldo_inicial_periodo = Decimal(str(cuenta.saldo_inicial)) + Decimal(str(entradas_previas)) - Decimal(str(salidas_previas))

    # 3. Obtener movimientos del mes ordenados cronológicamente
    transacciones_mes = db.query(Transaccion).filter(
        Transaccion.cuenta_id == cuenta_id,
        Transaccion.fecha >= fecha_inicio,
        Transaccion.fecha <= fecha_fin
    ).order_by(Transaccion.fecha.asc(), Transaccion.id.asc()).all()

    # 4. Calcular Saldo Progresivo acumulado renglón por renglón
    saldo_acumulado = saldo_inicial_periodo
    movimientos_formateados = []
    total_entradas = Decimal("0.00")
    total_salidas = Decimal("0.00")
    conteo_entradas = 0
    conteo_salidas = 0

    # Categorización agrupada para el resumen fiscal
    desglose_cat_dict = {}

    for t in transacciones_mes:
        monto = Decimal(str(t.monto))
        if t.tipo == "ENTRADA":
            saldo_acumulado += monto
            total_entradas += monto
            conteo_entradas += 1
        else:
            saldo_acumulado -= monto
            total_salidas += monto
            conteo_salidas += 1

        cat_nombre = t.categoria.nombre if t.categoria else "Sin Asignar"
        ent_nombre = t.entidad.nombre if t.entidad else None

        if cat_nombre not in desglose_cat_dict:
            desglose_cat_dict[cat_nombre] = {
                "categoria": cat_nombre,
                "tipo": t.tipo,
                "total": Decimal("0.00"),
                "conteo": 0
            }
        desglose_cat_dict[cat_nombre]["total"] += monto
        desglose_cat_dict[cat_nombre]["conteo"] += 1

        movimientos_formateados.append({
            "id": t.id,
            "fecha": t.fecha.isoformat(),
            "concepto": t.concepto,
            "referencia": t.referencia,
            "categoria_nombre": cat_nombre,
            "entidad_nombre": ent_nombre,
            "tipo": t.tipo,
            "monto": monto,
            "abono": monto if t.tipo == "ENTRADA" else None,
            "cargo": monto if t.tipo == "SALIDA" else None,
            "saldo_resultante": saldo_acumulado
        })

    saldo_final_periodo = saldo_acumulado

    # Ordenar desglose de categorías por mayor monto
    desglose_categorias = sorted(list(desglose_cat_dict.values()), key=lambda x: x["total"], reverse=True)

    return {
        "cuenta": {
            "id": cuenta.id,
            "nombre": cuenta.nombre,
            "rfc_empresa": cuenta.rfc_empresa,
            "logo_empresa": cuenta.logo_empresa,
            "banco": cuenta.banco,
            "logo_banco": cuenta.logo_banco,
            "clabe": cuenta.clabe,
            "numero_cuenta": cuenta.numero_cuenta,
            "moneda": cuenta.moneda
        },
        "periodo": {
            "mes": mes,
            "mes_nombre": NOMBRE_MESES.get(mes, ""),
            "anio": anio,
            "fecha_inicio": fecha_inicio.isoformat(),
            "fecha_fin": fecha_fin.isoformat()
        },
        "resumen": {
            "saldo_inicial": saldo_inicial_periodo,
            "total_entradas": total_entradas,
            "conteo_entradas": conteo_entradas,
            "total_salidas": total_salidas,
            "conteo_salidas": conteo_salidas,
            "variacion_neta": total_entradas - total_salidas,
            "saldo_final": saldo_final_periodo,
            "total_movimientos": len(transacciones_mes)
        },
        "movimientos": movimientos_formateados,
        "desglose_categorias": desglose_categorias
    }

# ====================================================================================================
# ARCHIVOS DE ESTADOS DE CUENTA (PDFS OFICIALES POR CUENTA, AÑO Y MES)
# ====================================================================================================
STORAGE_EDOS_DIR = os.path.join(os.path.dirname(__file__), "storage", "estados_cuenta")
os.makedirs(STORAGE_EDOS_DIR, exist_ok=True)

@app.post("/api/archivos-estado-cuenta", response_model=EstadoCuentaArchivoResponse)
async def subir_archivo_estado_cuenta(
    cuenta_id: int = Form(...),
    anio: int = Form(...),
    mes: int = Form(...),
    notas: Optional[str] = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    cuenta = db.query(Cuenta).filter(Cuenta.id == cuenta_id).first()
    if not cuenta:
        raise HTTPException(status_code=404, detail="Cuenta bancaria no encontrada.")

    if not file.filename:
        raise HTTPException(status_code=400, detail="No se seleccionó ningún archivo.")

    ext = os.path.splitext(file.filename)[1].lower()
    if ext != ".pdf":
        raise HTTPException(status_code=400, detail="Únicamente se permiten archivos PDF del Estado de Cuenta Bancario.")

    clean_filename = f"edo_{cuenta_id}_{anio}_{int(mes):02d}_{secrets.token_hex(4)}.pdf"
    file_path = os.path.join(STORAGE_EDOS_DIR, clean_filename)

    content = await file.read()
    with open(file_path, "wb") as f:
        f.write(content)

    nuevo_archivo = EstadoCuentaArchivo(
        cuenta_id=cuenta_id,
        anio=anio,
        mes=mes,
        nombre_archivo_original=file.filename,
        nombre_archivo_almacenado=clean_filename,
        ruta_archivo=file_path,
        tamano_bytes=len(content),
        notas=notas.strip() if notas else None
    )
    db.add(nuevo_archivo)
    db.commit()
    db.refresh(nuevo_archivo)

    res = EstadoCuentaArchivoResponse.model_validate(nuevo_archivo)
    res.cuenta_nombre = cuenta.nombre
    res.cuenta_banco = cuenta.banco
    res.cuenta_numero = cuenta.numero_cuenta
    return res

@app.get("/api/archivos-estado-cuenta", response_model=List[EstadoCuentaArchivoResponse])
def listar_archivos_estado_cuenta(
    cuenta_id: Optional[int] = None,
    anio: Optional[int] = None,
    mes: Optional[int] = None,
    db: Session = Depends(get_db)
):
    query = db.query(EstadoCuentaArchivo).join(Cuenta, EstadoCuentaArchivo.cuenta_id == Cuenta.id)
    if cuenta_id:
        query = query.filter(EstadoCuentaArchivo.cuenta_id == cuenta_id)
    if anio:
        query = query.filter(EstadoCuentaArchivo.anio == anio)
    if mes:
        query = query.filter(EstadoCuentaArchivo.mes == mes)

    archivos = query.order_by(EstadoCuentaArchivo.anio.desc(), EstadoCuentaArchivo.mes.desc(), EstadoCuentaArchivo.id.desc()).all()
    resultado = []
    for a in archivos:
        item = EstadoCuentaArchivoResponse.model_validate(a)
        if a.cuenta:
            item.cuenta_nombre = a.cuenta.nombre
            item.cuenta_banco = a.cuenta.banco
            item.cuenta_numero = a.cuenta.numero_cuenta
        resultado.append(item)
    return resultado

@app.get("/api/archivos-estado-cuenta/{archivo_id}/descargar")
def descargar_archivo_estado_cuenta(archivo_id: int, db: Session = Depends(get_db)):
    archivo = db.query(EstadoCuentaArchivo).filter(EstadoCuentaArchivo.id == archivo_id).first()
    if not archivo:
        raise HTTPException(status_code=404, detail="Archivo de estado de cuenta no encontrado.")

    if not os.path.exists(archivo.ruta_archivo):
        raise HTTPException(status_code=404, detail="El archivo físico no se encuentra en el servidor.")

    return FileResponse(
        path=archivo.ruta_archivo,
        filename=archivo.nombre_archivo_original,
        media_type="application/pdf"
    )

@app.get("/api/archivos-estado-cuenta/{archivo_id}/ver")
def ver_archivo_estado_cuenta_inline(archivo_id: int, db: Session = Depends(get_db)):
    archivo = db.query(EstadoCuentaArchivo).filter(EstadoCuentaArchivo.id == archivo_id).first()
    if not archivo:
        raise HTTPException(status_code=404, detail="Archivo de estado de cuenta no encontrado.")

    if not os.path.exists(archivo.ruta_archivo):
        raise HTTPException(status_code=404, detail="El archivo físico no se encuentra en el servidor.")

    return FileResponse(
        path=archivo.ruta_archivo,
        media_type="application/pdf"
    )

@app.delete("/api/archivos-estado-cuenta/{archivo_id}")
def eliminar_archivo_estado_cuenta(archivo_id: int, db: Session = Depends(get_db)):
    archivo = db.query(EstadoCuentaArchivo).filter(EstadoCuentaArchivo.id == archivo_id).first()
    if not archivo:
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")

    try:
        if os.path.exists(archivo.ruta_archivo):
            os.remove(archivo.ruta_archivo)
    except Exception:
        pass

    db.delete(archivo)
    db.commit()
    return {"message": "Archivo eliminado con éxito."}

# Montar frontend estático
frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "static")
if os.path.exists(frontend_dir):
    app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

    @app.get("/")
    def index():
        response = FileResponse(os.path.join(frontend_dir, "index.html"))
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
