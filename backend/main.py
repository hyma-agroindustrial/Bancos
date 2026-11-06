import os
import hashlib
import secrets
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, and_
from typing import List, Optional
from datetime import date
from decimal import Decimal

from .database import engine, Base, get_db
from .models import Cuenta, Categoria, Entidad, Transaccion, Usuario
from .schemas import (
    CuentaCreate, CuentaUpdate, CuentaResponse,
    CategoriaCreate, CategoriaResponse,
    EntidadCreate, EntidadResponse,
    TransaccionCreate, TransaccionResponse,
    MovimientoExtraido, BatchImportConfirm,
    UsuarioLogin, UsuarioCreate, UsuarioResponse, AuthResponse
)
from .parsers.bank_pdf_parser import extract_movements_from_pdf

# Inicializar Base de Datos
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Hyma Agroindustrial - Control Fiscal & Gastos",
    version="1.1.0",
    description="Sistema contable automatizado con control de usuarios por PIN, ingesta de estados de cuenta PDF y conciliación de cuentas fiscales."
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def hash_pin(pin: str) -> str:
    """Cifra el PIN de 4-8 dígitos con salteado criptográfico."""
    return hashlib.sha256(f"hyma_bancos_salt_{pin}".encode("utf-8")).hexdigest()

# SEED DATA AL INICIO
@app.on_event("startup")
def startup_seed_data():
    db = next(get_db())
    try:
        # 1. Seed Usuario Administrador por defecto
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

        # 2. Seed Categorías iniciales
        if db.query(Categoria).count() == 0:
            default_cats = [
                Categoria(nombre="Depósito Cliente", tipo="ENTRADA", tipo_entidad_sugerida="CLIENTE", descripcion="Cobranza de cosechas y productos agrícolas"),
                Categoria(nombre="Pago Gasolina", tipo="SALIDA", tipo_entidad_sugerida="GASOLINERA", descripcion="Combustible tractores, maquinaria y transporte"),
                Categoria(nombre="Pago Proveedor Insumos", tipo="SALIDA", tipo_entidad_sugerida="PROVEEDOR", descripcion="Fertilizantes, agroquímicos, semillas"),
                Categoria(nombre="Pago Nómina / Jornales", tipo="SALIDA", tipo_entidad_sugerida="EMPLEADO", descripcion="Pago a personal de campo y administrativo"),
                Categoria(nombre="Impuestos SAT / IMSS", tipo="SALIDA", tipo_entidad_sugerida="SAT", descripcion="Pagos provisionales, IVA, retenciones"),
                Categoria(nombre="Traspaso Entre Cuentas", tipo="AMBOS", tipo_entidad_sugerida="GENERAL", descripcion="Movimientos internos entre cuentas de la empresa"),
            ]
            db.add_all(default_cats)
            db.commit()

        # 3. Seed cuenta demo si no existe ninguna
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
# 0. CONTROL DE ACCESO (USUARIO + PIN) PARA ACCESO DESDE CUALQUIER DISPOSITIVO
# ====================================================================================================
@app.post("/api/auth/login", response_model=AuthResponse)
def login_usuario(payload: UsuarioLogin, db: Session = Depends(get_db)):
    user = db.query(Usuario).filter(Usuario.username == payload.username.strip(), Usuario.activo == True).first()
    if not user or user.pin_hash != hash_pin(payload.pin.strip()):
        raise HTTPException(status_code=401, detail="Usuario o PIN incorrecto.")

    # Generar token de sesión persistente para celular / tablet
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
        activo=True
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    return nuevo

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
    """Permite modificar nombre, banco, CLABE, número de cuenta o saldo inicial."""
    cuenta = db.query(Cuenta).filter(Cuenta.id == cuenta_id).first()
    if not cuenta:
        raise HTTPException(status_code=404, detail="Cuenta bancaria no encontrada.")

    # Validar que si cambia CLABE o número de cuenta, no colisione con otra
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
    if datos.banco is not None:
        cuenta.banco = datos.banco.strip()
    if datos.saldo_inicial is not None:
        cuenta.saldo_inicial = datos.saldo_inicial
    if datos.moneda is not None:
        cuenta.moneda = datos.moneda
    if datos.activo is not None:
        cuenta.activo = datos.activo

    db.commit()
    db.refresh(cuenta)

    # Recalcular saldo
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
    """Elimina o desactiva la cuenta bancaria para mantener integridad fiscal."""
    cuenta = db.query(Cuenta).filter(Cuenta.id == cuenta_id).first()
    if not cuenta:
        raise HTTPException(status_code=404, detail="Cuenta no encontrada.")

    # Marcar como inactiva (Soft-delete) para conservar historial fiscal de transacciones
    cuenta.activo = False
    db.commit()
    return {"message": "Cuenta bancaria eliminada del catálogo activo con éxito."}


# ====================================================================================================
# 2. CATEGORÍAS & ENTIDADES (CLIENTES / PROVEEDORES / GASOLINERAS)
# ====================================================================================================
@app.get("/api/categorias", response_model=List[CategoriaResponse])
def listar_categorias(tipo: Optional[str] = None, db: Session = Depends(get_db)):
    query = db.query(Categoria).filter(Categoria.activo == True)
    if tipo:
        query = query.filter((Categoria.tipo == tipo) | (Categoria.tipo == "AMBOS"))
    return query.order_by(Categoria.nombre).all()

@app.post("/api/categorias", response_model=CategoriaResponse)
def crear_categoria(cat: CategoriaCreate, db: Session = Depends(get_db)):
    existente = db.query(Categoria).filter(Categoria.nombre == cat.nombre).first()
    if existente:
        raise HTTPException(status_code=400, detail="Esta categoría ya existe.")
    nueva = Categoria(**cat.model_dump())
    db.add(nueva)
    db.commit()
    db.refresh(nueva)
    return nueva

@app.get("/api/entidades", response_model=List[EntidadResponse])
def listar_entidades(tipo_entidad: Optional[str] = None, db: Session = Depends(get_db)):
    query = db.query(Entidad).filter(Entidad.activo == True)
    if tipo_entidad and tipo_entidad != "GENERAL":
        query = query.filter(Entidad.tipo_entidad == tipo_entidad)
    return query.order_by(Entidad.nombre).all()

@app.post("/api/entidades", response_model=EntidadResponse)
def crear_entidad_al_vuelo(entidad: EntidadCreate, db: Session = Depends(get_db)):
    existente = db.query(Entidad).filter(
        Entidad.nombre == entidad.nombre,
        Entidad.tipo_entidad == entidad.tipo_entidad
    ).first()
    if existente:
        return existente
    
    nueva = Entidad(**entidad.model_dump())
    db.add(nueva)
    db.commit()
    db.refresh(nueva)
    return nueva


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
    tr_dict.categoria_nombre = nueva.categoria.nombre if nueva.categoria else None
    tr_dict.entidad_nombre = nueva.entidad.nombre if nueva.entidad else None
    return tr_dict


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
    gas_cat = next((c.id for c in cats if "GASOLINA" in c.nombre.upper()), None)
    cli_cat = next((c.id for c in cats if "CLIENTE" in c.nombre.upper()), None)
    sat_cat = next((c.id for c in cats if "IMPUESTO" in c.nombre.upper() or "SAT" in c.nombre.upper()), None)
    prov_cat = next((c.id for c in cats if "PROVEEDOR" in c.nombre.upper()), None)

    for m in movimientos:
        conc = m["concepto"].upper()
        if any(w in conc for w in ["GASOL", "COMBUSTIBLE", "HIDROCARB", "OXXO GAS", "PEMEX"]):
            m["categoria_id_sugerida"] = gas_cat
        elif any(w in conc for w in ["SAT", "IMPUESTO", "TESORERIA", "IMSS"]):
            m["categoria_id_sugerida"] = sat_cat
        elif m["tipo"] == "ENTRADA":
            m["categoria_id_sugerida"] = cli_cat
        elif m["tipo"] == "SALIDA":
            m["categoria_id_sugerida"] = prov_cat

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

# Montar frontend estático
frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "static")
if os.path.exists(frontend_dir):
    app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

    @app.get("/")
    def index():
        return FileResponse(os.path.join(frontend_dir, "index.html"))
