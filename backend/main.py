import os
import re
import json
import hashlib
import hmac
import time
import secrets
import threading
import calendar
from datetime import datetime, date
from decimal import Decimal
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Query, Header, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, text
from typing import List, Optional, Dict

from .database import engine, Base, get_db, IS_SQLITE, SessionLocal
from .cloud_storage import (
    ensure_buckets_exist,
    is_cloud_storage_enabled,
    save_estado_cuenta_pdf,
    resolve_or_fetch_estado_cuenta_pdf,
    remove_estado_cuenta_pdf,
    save_branding_image,
)
from .models import (
    Cuenta, Categoria, Entidad, Transaccion, Usuario, Configuracion,
    EstadoCuentaArchivo, ReglaClasificacion, CierreMes
)
from .schemas import (
    CuentaCreate, CuentaUpdate, CuentaResponse,
    CategoriaCreate, CategoriaUpdate, CategoriaResponse,
    EntidadCreate, EntidadUpdate, EntidadResponse,
    TransaccionCreate, TransaccionUpdate, TransaccionResponse,
    BulkTransaccionUpdate, ConciliacionCierrePayload,
    MovimientoExtraido, BatchImportConfirm,
    UsuarioLogin, UsuarioCreate, UsuarioUpdate, UsuarioResponse, AuthResponse,
    ConfiguracionData, EstadoCuentaArchivoResponse
)
from .autocategorizar import construir_historial, pistas as pistas_de, sugerir as sugerir_categoria, _TIPO_ETIQUETA
from .parsers.bank_pdf_parser import extract_movements_from_pdf, generate_movement_hash

# Inicializar Base de Datos
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Consolidado de Bancos - Control Fiscal & Gastos",
    version="1.2.0",
    description="Sistema contable automatizado con personalización de marca, captura al vuelo de categorías y conciliación bancaria."
)

def _origenes_permitidos() -> list:
    """CORS restringido. La app web se sirve desde el mismo origen, asi que solo se permiten
    los orígenes locales, el dominio público de Render (si existe) y los de CORS_ORIGINS."""
    lista = ["http://localhost:8000", "http://127.0.0.1:8000"]
    extra = os.environ.get("CORS_ORIGINS", "")
    lista += [o.strip().rstrip("/") for o in extra.split(",") if o.strip()]
    render_url = (os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if render_url:
        lista.append(render_url)
    return lista


app.add_middleware(
    CORSMiddleware,
    allow_origins=_origenes_permitidos(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------------------------------------------------------------------------------
# SEGURIDAD DE LOGIN: PIN con PBKDF2-SHA256 + sal aleatoria por usuario, límite de intentos y
# caducidad de sesión. Los PIN antiguos (SHA-256 con sal fija) se aceptan una vez y se migran solos.
# Formato nuevo en pin_hash (cabe en String(128)):  p2$<iteraciones>$<sal hex>$<hash hex>
# ----------------------------------------------------------------------------------------------------
PBKDF2_ITER = 240_000
SESION_HORAS = float(os.environ.get("SESION_HORAS", "12"))
LOGIN_MAX_FALLOS = int(os.environ.get("LOGIN_MAX_FALLOS", "5"))
LOGIN_BLOQUEO_SEG = int(os.environ.get("LOGIN_BLOQUEO_MIN", "15")) * 60


def _hash_legacy(pin: str) -> str:
    return hashlib.sha256(f"hyma_bancos_salt_{pin}".encode("utf-8")).hexdigest()


def hash_pin(pin: str) -> str:
    sal = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), sal, PBKDF2_ITER)
    return f"p2${PBKDF2_ITER}${sal.hex()}${dk.hex()}"


def _pin_es_moderno(almacenado: str) -> bool:
    return (almacenado or "").startswith("p2$")


def verificar_pin(pin: str, almacenado: str) -> bool:
    almacenado = almacenado or ""
    if _pin_es_moderno(almacenado):
        try:
            _, iters, sal_hex, hash_hex = almacenado.split("$")
            dk = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(sal_hex), int(iters))
            return hmac.compare_digest(dk.hex(), hash_hex)
        except Exception:
            return False
    return hmac.compare_digest(_hash_legacy(pin), almacenado)


_HASH_DUMMY = hash_pin("00000000")  # para igualar el tiempo cuando el usuario no existe


def nuevo_token_sesion() -> str:
    """56 caracteres: 8 hex con la hora de expiración (epoch) + 48 hex aleatorios.
    Cabe en token_sesion (String(64)). El token solo vale si coincide con el guardado en BD."""
    expira = int(time.time() + SESION_HORAS * 3600)
    return f"{expira:08x}{secrets.token_hex(24)}"


def token_vigente(token: str) -> bool:
    if len(token) != 56:       # formato antiguo (sin caducidad): obliga a iniciar sesión de nuevo
        return False
    try:
        return int(token[:8], 16) > time.time()
    except ValueError:
        return False


_intentos_login: Dict[str, list] = {}   # clave -> [fallos, bloqueado_hasta_epoch]
_intentos_lock = threading.Lock()


def _ip_cliente(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for") or ""
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _claves_intentos(request: Request, uname: str) -> list:
    return [f"ip:{_ip_cliente(request)}", f"user:{uname}"]


def _segundos_bloqueado(claves: list) -> int:
    ahora = time.time()
    with _intentos_lock:
        restante = 0
        for c in claves:
            reg = _intentos_login.get(c)
            if reg and reg[1] > ahora:
                restante = max(restante, int(reg[1] - ahora) + 1)
        return restante


def _registrar_fallo(claves: list) -> None:
    ahora = time.time()
    with _intentos_lock:
        for c in claves:
            reg = _intentos_login.get(c)
            if not reg or (reg[1] and reg[1] <= ahora):
                reg = [0, 0.0]
            reg[0] += 1
            if reg[0] >= LOGIN_MAX_FALLOS:
                reg[1] = ahora + LOGIN_BLOQUEO_SEG
                reg[0] = 0
            _intentos_login[c] = reg
        if len(_intentos_login) > 5000:   # evita crecimiento sin límite
            for k in [k for k, v in _intentos_login.items() if v[1] <= ahora and v[0] == 0]:
                _intentos_login.pop(k, None)


def _limpiar_fallos(claves: list) -> None:
    with _intentos_lock:
        for c in claves:
            _intentos_login.pop(c, None)


# ====================================================================================================
# CONTROL DE ACCESO EN EL SERVIDOR (sesión obligatoria, roles y alcance por cuenta bancaria)
#   ADMIN    -> acceso total.
#   OPERADOR -> captura y consulta SOLO en sus cuentas asignadas.
#   CONSULTA -> solo lectura en sus cuentas asignadas (ver, reportes, descargar). No captura ni modifica.
# ====================================================================================================
ROLES_VALIDOS = ("ADMIN", "OPERADOR", "CONSULTA")
_METODOS_ESCRITURA = {"POST", "PUT", "PATCH", "DELETE"}
_RUTAS_PUBLICAS = {
    ("POST", "/api/auth/login"),
    ("GET", "/api/health"),
    ("HEAD", "/api/health"),
    ("GET", "/api/configuracion"),
}


def _rol_de(u) -> str:
    return str(getattr(u, "rol", "") or "OPERADOR").upper()


def _ids_cuentas_de(u) -> set:
    try:
        return {int(x) for x in json.loads(getattr(u, "cuentas_permitidas", None) or "[]")}
    except Exception:
        return set()


def _ruta_solo_admin(method: str, path: str) -> bool:
    if path.startswith("/api/usuarios") or path.startswith("/api/backup"):
        return True
    if path.startswith("/api/configuracion") and method != "GET":
        return True
    if path.startswith("/api/cuentas") and method in _METODOS_ESCRITURA:
        return True
    if method == "DELETE" and path.startswith(("/api/categorias", "/api/entidades", "/api/archivos-estado-cuenta")):
        return True
    return False


@app.middleware("http")
async def control_de_acceso(request: Request, call_next):
    path = request.url.path
    method = request.method.upper()
    if not path.startswith("/api/") or method == "OPTIONS" or (method, path) in _RUTAS_PUBLICAS:
        return await call_next(request)

    auth = request.headers.get("authorization") or ""
    token = auth.replace("Bearer ", "").strip() or (request.query_params.get("token") or "").strip()
    if not token:
        return JSONResponse({"detail": "Sesión requerida. Inicia sesión de nuevo."}, status_code=401)

    if not token_vigente(token):
        return JSONResponse({"detail": "Sesión expirada. Inicia sesión de nuevo."}, status_code=401)

    db = SessionLocal()
    try:
        user = db.query(Usuario).filter(Usuario.token_sesion == token, Usuario.activo == True).first()
        if user is not None:
            db.expunge(user)
    finally:
        db.close()
    if user is None:
        return JSONResponse({"detail": "Sesión inválida o expirada. Inicia sesión de nuevo."}, status_code=401)

    rol = _rol_de(user)
    if rol != "ADMIN":
        if _ruta_solo_admin(method, path):
            return JSONResponse({"detail": "Esta acción es exclusiva del Administrador."}, status_code=403)
        if rol == "CONSULTA" and method in _METODOS_ESCRITURA:
            return JSONResponse({"detail": "Tu usuario es de SOLO LECTURA: puedes consultar y descargar, pero no capturar ni modificar."}, status_code=403)

    request.state.usuario = user
    return await call_next(request)


def _alcance_cuentas(request: Request) -> Optional[set]:
    """None = sin restricción (ADMIN). Un set = IDs de cuentas a las que el usuario tiene acceso."""
    u = getattr(request.state, "usuario", None)
    if u is None or _rol_de(u) == "ADMIN":
        return None
    return _ids_cuentas_de(u)


def _exigir_cuenta(request: Request, cuenta_id) -> None:
    alcance = _alcance_cuentas(request)
    if alcance is not None and (cuenta_id is None or int(cuenta_id) not in alcance):
        raise HTTPException(status_code=403, detail="No tienes acceso a esta cuenta bancaria.")


def _usuario_actual(request: Request) -> Optional[str]:
    u = getattr(request.state, "usuario", None)
    return u.username if u is not None else None

# SEED DATA AL INICIO
@app.on_event("startup")
def startup_seed_data():
    ensure_buckets_exist()
    # Iniciar hilo Keep-Alive 24/7 en la nube para evitar suspensión en Render Free y Supabase
    if not IS_SQLITE:
        import threading
        import time
        import urllib.request

        def _keep_alive_worker():
            target_url = os.getenv("RENDER_EXTERNAL_URL", "https://bancos-9yfp.onrender.com").rstrip("/") + "/api/health"
            while True:
                time.sleep(600)  # Cada 10 minutos
                try:
                    with urllib.request.urlopen(target_url, timeout=15) as _:
                        pass
                except Exception:
                    pass

        threading.Thread(target=_keep_alive_worker, daemon=True).start()

    db = next(get_db())
    try:
        # 0. Migración segura de columnas nuevas en SQLite
        if IS_SQLITE:
            for col_sql in [
                "ALTER TABLE transacciones ADD COLUMN estatus_factura VARCHAR(20) DEFAULT 'PENDIENTE'",
                "ALTER TABLE transacciones ADD COLUMN usuario_registro VARCHAR(80)",
                "ALTER TABLE transacciones ADD COLUMN traspaso_vinculado_id INTEGER"
            ]:
                try:
                    db.execute(text(col_sql))
                    db.commit()
                except Exception:
                    db.rollback()

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
            pin_inicial = (os.environ.get("ADMIN_PIN_INICIAL") or "").strip()
            if not (pin_inicial.isdigit() and 4 <= len(pin_inicial) <= 8):
                pin_inicial = "".join(secrets.choice("0123456789") for _ in range(6))
                print(f"[SEGURIDAD] Usuario 'admin' creado con PIN inicial aleatorio: {pin_inicial}  (cámbialo al entrar)", flush=True)
            admin_user = Usuario(
                username="admin",
                nombre="Administrador General",
                pin_hash=hash_pin(pin_inicial),
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
# MONITOR DE SALUD Y KEEP-ALIVE 24/7 (PARA RENDER FREE Y SUPABASE)
# ====================================================================================================
@app.get("/api/health")
def health_check(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {
        "status": "ok",
        "database": "sqlite" if IS_SQLITE else "postgresql",
        "cloud_storage": is_cloud_storage_enabled(),
        "timestamp": datetime.now().isoformat()
    }


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
    clean_filename = f"logo_{secrets.token_hex(4)}{ext}"
    content = await file.read()
    logo_url = save_branding_image(clean_filename, content, ext, uploads_dir)

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
def login_usuario(payload: UsuarioLogin, request: Request, db: Session = Depends(get_db)):
    uname = (payload.username or "").strip().lower()
    claves = _claves_intentos(request, uname)
    espera = _segundos_bloqueado(claves)
    if espera:
        raise HTTPException(
            status_code=429,
            detail=f"Demasiados intentos fallidos. Intenta de nuevo en {max(1, espera // 60)} min.",
        )

    pin = (payload.pin or "").strip()
    user = db.query(Usuario).filter(func.lower(Usuario.username) == uname, Usuario.activo == True).first()
    if user is None:
        verificar_pin(pin, _HASH_DUMMY)   # mismo costo de tiempo para no revelar si el usuario existe
        ok = False
    else:
        ok = verificar_pin(pin, user.pin_hash)
    if not ok:
        _registrar_fallo(claves)
        raise HTTPException(status_code=401, detail="Usuario o PIN incorrecto.")

    _limpiar_fallos(claves)
    if not _pin_es_moderno(user.pin_hash):      # migración transparente al hash fuerte
        user.pin_hash = hash_pin(pin)

    token = nuevo_token_sesion()
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
    if not token_vigente(token):
        raise HTTPException(status_code=401, detail="Sesión inválida o expirada.")
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
        user.token_sesion = None   # un PIN nuevo cierra cualquier sesión abierta
    if payload.rol is not None:
        if user.username == "admin" and payload.rol != "ADMIN":
            raise HTTPException(status_code=400, detail="El administrador principal siempre debe mantener rol ADMIN.")
        user.rol = payload.rol
    if payload.activo is not None:
        user.activo = payload.activo
        if not payload.activo:
            user.token_sesion = None
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
def listar_cuentas(request: Request, db: Session = Depends(get_db)):
    cuentas = db.query(Cuenta).filter(Cuenta.activo == True).all()
    alcance = _alcance_cuentas(request)
    if alcance is not None:
        cuentas = [c for c in cuentas if c.id in alcance]
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
# 3. CAPTURA Y GESTIÓN DE TRANSACCIONES (CON AUTO-APRENDIZAJE, TRASPASO ESPEJO Y CANDADO DE MES)
# ====================================================================================================
NOMBRE_MESES_CORTO = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
    5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"
}

def verificar_mes_cerrado(db: Session, cuenta_id: int, f_date: date):
    cierre = db.query(CierreMes).filter(
        CierreMes.cuenta_id == cuenta_id,
        CierreMes.anio == f_date.year,
        CierreMes.mes == f_date.month,
        CierreMes.cerrado == True
    ).first()
    if cierre:
        mes_nom = NOMBRE_MESES_CORTO.get(f_date.month, str(f_date.month))
        raise HTTPException(
            status_code=400,
            detail=f"🔒 El periodo {mes_nom} {f_date.year} de esta cuenta está CERRADO y CONCILIADO. Desbloquéalo en 'Corte de Mes' si necesitas hacer ajustes."
        )

def extraer_patron_aprendizaje(concepto: str) -> Optional[str]:
    if not concepto:
        return None
    c_up = concepto.strip().upper()
    # 1. Si el concepto incluye un RFC explícito, usar ese RFC como llave de alta precisión
    rfc_match = re.search(r"\b([A-Z&Ñ]{3,4}\s?\d{6}[A-Z0-9]{3})\b", c_up)
    if rfc_match:
        return rfc_match.group(1).replace(" ", "")
    # 2. Limpiar folios largos, horas y referencias numéricas variables
    limpio = re.sub(r"\b\d{4,}\b", "", c_up)
    limpio = re.sub(r"\d{2}:\d{2}(:\d{2})?", "", limpio)
    limpio = re.sub(r"[^A-ZÁÉÍÓÚÑ\s]", " ", limpio)
    limpio = re.sub(r"\s+", " ", limpio).strip()
    if len(limpio) >= 5:
        return limpio[:65]
    return None

def aprender_regla_clasificacion(
    db: Session,
    concepto: str,
    categoria_id: Optional[int],
    entidad_id: Optional[int]
):
    try:
        if not categoria_id and not entidad_id:
            return
        patron = extraer_patron_aprendizaje(concepto)
        if not patron or len(patron) < 4:
            return
        # Evitar guardar patrones genéricos de traspaso que cambian de destino
        if patron in ("SPEI", "TRANSFERENCIA", "ABONO", "CARGO"):
            return

        # 1. Verificar si ya fue agregado en la sesión pendiente (evita colisiones intra-lote UNIQUE)
        for obj in list(db.new):
            if isinstance(obj, ReglaClasificacion) and obj.patron == patron:
                if categoria_id is not None:
                    obj.categoria_id = categoria_id
                if entidad_id is not None:
                    obj.entidad_id = entidad_id
                return

        # 2. Buscar en la base de datos
        regla = db.query(ReglaClasificacion).filter(ReglaClasificacion.patron == patron).first()
        if regla:
            if categoria_id is not None:
                regla.categoria_id = categoria_id
            if entidad_id is not None:
                regla.entidad_id = entidad_id
        else:
            db.add(ReglaClasificacion(
                patron=patron,
                categoria_id=categoria_id,
                entidad_id=entidad_id
            ))
    except Exception:
        pass

def inferir_estatus_factura_default(concepto: str, categoria_nombre: Optional[str] = None) -> str:
    c_up = (concepto or "").upper()
    cat_up = (categoria_nombre or "").upper()
    if any(w in c_up for w in ["COMISION", "IVA - COMISIONES", "TRASPASO", "MEMBRESIA", "IMPUESTO", "SAT", "NOMINA"]) or any(
        w in cat_up for w in ["COMISION", "TRASPASO", "IMPUESTO", "NOMINA"]
    ):
        return "NO_APLICA"
    return "PENDIENTE"

@app.get("/api/transacciones", response_model=List[TransaccionResponse])
def listar_transacciones(
    cuenta_id: Optional[int] = None,
    categoria_id: Optional[int] = None,
    entidad_id: Optional[int] = None,
    tipo: Optional[str] = None,
    estatus_factura: Optional[str] = None,
    fecha_inicio: Optional[date] = None,
    fecha_fin: Optional[date] = None,
    limit: int = 2000,
    request: Request = None,
    db: Session = Depends(get_db)
):
    query = db.query(Transaccion)
    alcance = _alcance_cuentas(request)

    if cuenta_id:
        _exigir_cuenta(request, cuenta_id)
        query = query.filter(Transaccion.cuenta_id == cuenta_id)
    elif alcance is not None:
        query = query.filter(Transaccion.cuenta_id.in_(alcance or [-1]))
    if categoria_id:
        query = query.filter(Transaccion.categoria_id == categoria_id)
    if entidad_id:
        query = query.filter(Transaccion.entidad_id == entidad_id)
    if tipo:
        query = query.filter(Transaccion.tipo == tipo)
    if estatus_factura:
        query = query.filter(Transaccion.estatus_factura == estatus_factura)
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
        if not tr_dict.estatus_factura:
            tr_dict.estatus_factura = "PENDIENTE"
        resultado.append(tr_dict)
        
    return resultado

@app.post("/api/transacciones", response_model=TransaccionResponse)
def crear_transaccion_manual(trans: TransaccionCreate, request: Request, db: Session = Depends(get_db)):
    _exigir_cuenta(request, trans.cuenta_id)
    if _usuario_actual(request):
        trans.usuario_registro = _usuario_actual(request)
    cuenta = db.query(Cuenta).filter(Cuenta.id == trans.cuenta_id).first()
    if not cuenta:
        raise HTTPException(status_code=404, detail="Cuenta bancaria no encontrada.")

    verificar_mes_cerrado(db, trans.cuenta_id, trans.fecha)

    datos_insert = trans.model_dump(exclude={"cuenta_destino_id"})
    if not datos_insert.get("estatus_factura"):
        cat_obj = db.query(Categoria).filter(Categoria.id == trans.categoria_id).first() if trans.categoria_id else None
        datos_insert["estatus_factura"] = inferir_estatus_factura_default(
            trans.concepto, cat_obj.nombre if cat_obj else None
        )

    nueva = Transaccion(**datos_insert)
    db.add(nueva)
    db.flush()

    # Si es un Traspaso Entre Cuentas con Cuenta Destino seleccionada, crear el Movimiento Espejo Automático
    if trans.cuenta_destino_id and int(trans.cuenta_destino_id) != int(trans.cuenta_id):
        cuenta_dest = db.query(Cuenta).filter(Cuenta.id == trans.cuenta_destino_id).first()
        if not cuenta_dest:
            raise HTTPException(status_code=404, detail="La cuenta bancaria destino del traspaso no existe.")
        verificar_mes_cerrado(db, cuenta_dest.id, trans.fecha)

        tipo_espejo = "ENTRADA" if trans.tipo == "SALIDA" else "SALIDA"
        concepto_espejo = f"TRASPASO {'DE' if tipo_espejo == 'ENTRADA' else 'A'} {cuenta.banco} ({cuenta.nombre} *{cuenta.numero_cuenta[-4:]}) · {trans.concepto}"
        espejo = Transaccion(
            cuenta_id=cuenta_dest.id,
            categoria_id=trans.categoria_id,
            entidad_id=trans.entidad_id,
            fecha=trans.fecha,
            tipo=tipo_espejo,
            monto=trans.monto,
            concepto=concepto_espejo[:250],
            referencia=trans.referencia,
            origen="MANUAL",
            estatus_factura="NO_APLICA",
            usuario_registro=trans.usuario_registro,
            traspaso_vinculado_id=nueva.id
        )
        db.add(espejo)
        db.flush()
        nueva.traspaso_vinculado_id = espejo.id
        nueva.estatus_factura = "NO_APLICA"

    aprender_regla_clasificacion(db, nueva.concepto, nueva.categoria_id, nueva.entidad_id)
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
def modificar_transaccion(transaccion_id: int, datos: TransaccionUpdate, request: Request, db: Session = Depends(get_db)):
    """Permite modificar fecha, monto, concepto, categoría, cuenta, estatus CFDI o beneficiario de un movimiento existente."""
    t = db.query(Transaccion).filter(Transaccion.id == transaccion_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Movimiento no encontrado.")
    _exigir_cuenta(request, t.cuenta_id)

    verificar_mes_cerrado(db, t.cuenta_id, t.fecha)

    if datos.cuenta_id is not None:
        cta = db.query(Cuenta).filter(Cuenta.id == datos.cuenta_id).first()
        if not cta:
            raise HTTPException(status_code=404, detail="Cuenta bancaria no válida.")
        _exigir_cuenta(request, datos.cuenta_id)
        if int(datos.cuenta_id) != int(t.cuenta_id):
            verificar_mes_cerrado(db, datos.cuenta_id, datos.fecha or t.fecha)
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
        verificar_mes_cerrado(db, t.cuenta_id, datos.fecha)
        t.fecha = datos.fecha
    if datos.tipo is not None:
        t.tipo = datos.tipo
    if datos.monto is not None:
        t.monto = datos.monto
    if datos.concepto is not None:
        t.concepto = datos.concepto.strip()
    if datos.referencia is not None:
        t.referencia = datos.referencia.strip()
    if datos.estatus_factura is not None:
        t.estatus_factura = datos.estatus_factura
    if datos.usuario_registro is not None:
        t.usuario_registro = datos.usuario_registro

    # Traspaso espejo: mantener la contrapartida idéntica (mismo monto/fecha, tipo opuesto) para no descuadrar la otra cuenta
    if t.traspaso_vinculado_id:
        espejo = db.query(Transaccion).filter(Transaccion.id == t.traspaso_vinculado_id).first()
        if espejo is not None:
            verificar_mes_cerrado(db, espejo.cuenta_id, espejo.fecha)
            if datos.fecha is not None:
                verificar_mes_cerrado(db, espejo.cuenta_id, datos.fecha)
            espejo.monto = t.monto
            espejo.fecha = t.fecha
            espejo.tipo = "ENTRADA" if t.tipo == "SALIDA" else "SALIDA"
            if datos.referencia is not None:
                espejo.referencia = t.referencia

    aprender_regla_clasificacion(db, t.concepto, t.categoria_id, t.entidad_id)
    db.commit()
    db.refresh(t)

    tr_dict = TransaccionResponse.model_validate(t)
    tr_dict.cuenta_nombre = t.cuenta.nombre if t.cuenta else None
    tr_dict.cuenta_banco = t.cuenta.banco if t.cuenta else None
    tr_dict.cuenta_numero = t.cuenta.numero_cuenta if t.cuenta else None
    tr_dict.categoria_nombre = t.categoria.nombre if t.categoria else None
    tr_dict.entidad_nombre = t.entidad.nombre if t.entidad else None
    return tr_dict

@app.patch("/api/transacciones/{transaccion_id}/factura")
def cambiar_estatus_factura(transaccion_id: int, request: Request, estatus_factura: str = Query(...), db: Session = Depends(get_db)):
    t = db.query(Transaccion).filter(Transaccion.id == transaccion_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Movimiento no encontrado.")
    _exigir_cuenta(request, t.cuenta_id)
    if estatus_factura not in ("FACTURADO", "PENDIENTE", "NO_APLICA"):
        raise HTTPException(status_code=400, detail="Estatus de factura inválido.")
    t.estatus_factura = estatus_factura
    db.commit()
    return {"id": t.id, "estatus_factura": t.estatus_factura}

@app.get("/api/transacciones/sugerencias-categoria")
def sugerencias_categoria(request: Request, db: Session = Depends(get_db)):
    """Agrupa los movimientos SIN categoría por huella (comercio/cuenta destino/palabra) y propone
    la categoría que el propio usuario ya usó antes. Solo lectura: aplicar usa /bulk-update."""
    alcance = _alcance_cuentas(request)
    q = db.query(Transaccion)
    if alcance is not None:
        q = q.filter(Transaccion.cuenta_id.in_(alcance or [-1]))
    todos = q.all()
    nombres = {c.id: c.nombre for c in db.query(Categoria).all()}
    historial = construir_historial(
        (t.concepto, t.categoria_id) for t in todos if t.categoria_id is not None and t.tipo == "SALIDA"
    )
    cierres = {
        (c.cuenta_id, c.anio, c.mes)
        for c in db.query(CierreMes).filter(CierreMes.cerrado == True).all()
    }
    grupos: Dict[tuple, dict] = {}
    sin_pistas = ambiguos = total_sin_cat = 0
    for t in todos:
        if t.categoria_id is not None or t.tipo != "SALIDA":
            continue
        total_sin_cat += 1
        sug = sugerir_categoria(t.concepto, historial)
        if sug is None:
            if any(historial.get(p) for p in pistas_de(t.concepto)):
                ambiguos += 1
            else:
                sin_pistas += 1
            continue
        clave = (sug["pista"], sug["categoria_id"])
        g = grupos.setdefault(clave, {
            "tipo_pista": _TIPO_ETIQUETA.get(sug["pista"][0], sug["pista"][0]),
            "pista": sug["pista"][1],
            "categoria_id": sug["categoria_id"],
            "categoria": nombres.get(sug["categoria_id"], "?"),
            "confianza": sug["confianza"],
            "veces_usada": sug["n"], "de": sug["total"],
            "ids": [], "bloqueados": 0, "total": 0.0, "ejemplo": (t.concepto or "")[:110],
        })
        if (t.cuenta_id, t.fecha.year, t.fecha.month) in cierres:
            g["bloqueados"] += 1
            continue
        g["ids"].append(t.id)
        g["total"] += float(t.monto or 0)
    lista = [g for g in grupos.values() if g["ids"]]
    for g in lista:
        g["cantidad"] = len(g["ids"])
        g["total"] = round(g["total"], 2)
    lista.sort(key=lambda g: (g["confianza"] != "alta", -g["total"]))
    return {
        "total_sin_categoria": total_sin_cat,
        "sugeridos": sum(g["cantidad"] for g in lista),
        "ambiguos": ambiguos,
        "sin_pistas": sin_pistas,
        "grupos": lista,
    }


@app.post("/api/transacciones/bulk-update")
def edicion_masiva_transacciones(payload: BulkTransaccionUpdate, request: Request, db: Session = Depends(get_db)):
    if not payload.ids:
        raise HTTPException(status_code=400, detail="Selecciona al menos un movimiento.")
    registros = db.query(Transaccion).filter(Transaccion.id.in_(payload.ids)).all()
    for t in registros:
        _exigir_cuenta(request, t.cuenta_id)
    if _usuario_actual(request):
        payload.usuario_registro = _usuario_actual(request)
    actualizados = 0
    for t in registros:
        verificar_mes_cerrado(db, t.cuenta_id, t.fecha)
        if payload.categoria_id is not None:
            t.categoria_id = payload.categoria_id
        if payload.entidad_id is not None:
            t.entidad_id = payload.entidad_id
        if payload.estatus_factura is not None and payload.estatus_factura != "":
            t.estatus_factura = payload.estatus_factura
        if payload.usuario_registro:
            t.usuario_registro = payload.usuario_registro
        aprender_regla_clasificacion(db, t.concepto, t.categoria_id, t.entidad_id)
        actualizados += 1
    db.commit()
    return {"message": f"✅ Se actualizaron {actualizados} movimientos correctamente.", "actualizados": actualizados}

@app.delete("/api/transacciones/{transaccion_id}")
def eliminar_transaccion(transaccion_id: int, request: Request, db: Session = Depends(get_db)):
    """Elimina permanentemente una transacción (y su contrapartida si es un traspaso espejo)."""
    t = db.query(Transaccion).filter(Transaccion.id == transaccion_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Movimiento no encontrado.")
    _exigir_cuenta(request, t.cuenta_id)
    verificar_mes_cerrado(db, t.cuenta_id, t.fecha)
    espejo = None
    if t.traspaso_vinculado_id:
        espejo = db.query(Transaccion).filter(Transaccion.id == t.traspaso_vinculado_id).first()
        if espejo is not None:
            verificar_mes_cerrado(db, espejo.cuenta_id, espejo.fecha)
    db.delete(t)
    if espejo is not None:
        db.delete(espejo)
    db.commit()
    return {"message": "Movimiento eliminado exitosamente." + (" También se eliminó su contrapartida del traspaso." if espejo is not None else "")}


# ====================================================================================================
# 4. EXTRACCIÓN AUTOMATIZADA DE PDF BANCARIO (ESTADOS DE CUENTA CON AUTO-APRENDIZAJE Y ARCHIVADO)
# ====================================================================================================
STORAGE_EDOS_DIR = os.path.join(os.path.dirname(__file__), "storage", "estados_cuenta")
os.makedirs(STORAGE_EDOS_DIR, exist_ok=True)
PENDING_PDF_UPLOADS: Dict[str, bytes] = {}

@app.post("/api/transacciones/upload-pdf", response_model=List[MovimientoExtraido])
async def extraer_movimientos_pdf(
    request: Request,
    cuenta_id: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    _exigir_cuenta(request, cuenta_id)
    cuenta = db.query(Cuenta).filter(Cuenta.id == cuenta_id).first()
    if not cuenta:
        raise HTTPException(status_code=404, detail="Cuenta bancaria especificada no existe.")

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="El archivo debe ser un documento PDF válido.")

    content = await file.read()
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="El archivo PDF está vacío.")

    # Guardar temporalmente los bytes del PDF para archivarlo automáticamente en "Estados de Cuenta" al confirmar importación
    PENDING_PDF_UPLOADS[f"{cuenta_id}:{file.filename}"] = content

    existing_hashes = set(
        h[0] for h in db.query(Transaccion.hash_deduplicacion).filter(
            Transaccion.cuenta_id == cuenta_id, Transaccion.hash_deduplicacion != None
        ).all()
    )

    movimientos = extract_movements_from_pdf(content, cuenta_id, existing_hashes)

    cats = db.query(Categoria).filter(Categoria.activo == True).all()
    entidades_db = db.query(Entidad).filter(Entidad.activo == True).all()
    reglas_aprendidas = db.query(ReglaClasificacion).all()

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
        m["regla_aprendida"] = False

        # 1. Aplicar primero Memoria Inteligente (Reglas Aprendidas por RFC o patrón previo)
        patron_mov = extraer_patron_aprendizaje(conc)
        for regla in reglas_aprendidas:
            if (patron_mov and regla.patron == patron_mov) or (len(regla.patron) >= 5 and regla.patron in conc):
                if regla.categoria_id:
                    m["categoria_id_sugerida"] = regla.categoria_id
                if regla.entidad_id:
                    m["entidad_id_sugerida"] = regla.entidad_id
                m["regla_aprendida"] = True
                break

        # 2. Si no hubo regla aprendida para categoría, usar heurística base
        if not m.get("categoria_id_sugerida"):
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
            elif any(w in conc for w in ["COMISION", "MEMBRESIA", "ORDEN DE PAGO SPEI", "TRANSFERENCIA - ENVIO", "ANUALIDAD", "DISPERSION DE FONDOS"]):
                m["categoria_id_sugerida"] = com_cat
            elif any(w in conc for w in ["SAT", "IMPUESTO", "TESORERIA", "IMSS"]):
                m["categoria_id_sugerida"] = sat_cat
            elif any(w in conc for w in ["TRASPASO", "SWEB TRASPASO"]):
                m["categoria_id_sugerida"] = trasp_cat
            elif any(w in conc for w in ["TOYOTA", "CAM TOYOTA", "REFACCION"]):
                m["categoria_id_sugerida"] = refac_cat
            elif m["tipo"] == "ENTRADA":
                m["categoria_id_sugerida"] = cli_cat

        # 3. Sugerir entidad por RFC o nombre si aún no se asignó
        if not m.get("entidad_id_sugerida"):
            for ent in entidades_db:
                ent_rfc = (ent.rfc or "").strip().upper()
                ent_nom = (ent.nombre or "").strip().upper()
                if (len(ent_rfc) >= 10 and ent_rfc in conc.replace(" ", "")) or (len(ent_nom) >= 4 and ent_nom in conc):
                    m["entidad_id_sugerida"] = ent.id
                    break

        m["estatus_factura"] = inferir_estatus_factura_default(conc)

    return movimientos

@app.post("/api/transacciones/batch-import")
def confirmar_importacion_lote(payload: BatchImportConfirm, request: Request, db: Session = Depends(get_db)):
    _exigir_cuenta(request, payload.cuenta_id)
    if _usuario_actual(request):
        payload.usuario_registro = _usuario_actual(request)
    insertados = 0
    ignorados = 0

    hashes_db = set(
        h[0] for h in db.query(Transaccion.hash_deduplicacion).filter(
            Transaccion.hash_deduplicacion != None
        ).all()
    )
    hashes_lote = set()
    occurrence_counts: Dict[str, int] = {}

    for m in payload.movimientos:
        if m.es_duplicado:
            ignorados += 1
            continue

        verificar_mes_cerrado(db, payload.cuenta_id, m.fecha)

        base_hash = m.hash_deduplicacion
        count = occurrence_counts.get(base_hash, 0) + 1
        occurrence_counts[base_hash] = count

        final_hash = base_hash
        if count > 1 or final_hash in hashes_lote:
            ref_part = m.referencia or ""
            base_concept = f"{m.concepto} {ref_part}".strip() if ref_part else m.concepto
            seq = count if count > 1 else 2
            while True:
                candidate = generate_movement_hash(
                    payload.cuenta_id,
                    m.fecha,
                    Decimal(str(m.monto)),
                    m.tipo,
                    f"{base_concept} #{seq}"
                )
                if candidate not in hashes_lote:
                    final_hash = candidate
                    break
                seq += 1

        if final_hash in hashes_db:
            ignorados += 1
            continue

        hashes_lote.add(final_hash)

        est_factura = m.estatus_factura or inferir_estatus_factura_default(m.concepto)

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
            hash_deduplicacion=final_hash,
            estatus_factura=est_factura,
            usuario_registro=payload.usuario_registro
        )
        db.add(nueva_trans)
        aprender_regla_clasificacion(db, m.concepto, m.categoria_id_sugerida, m.entidad_id_sugerida)
        insertados += 1

    # Archivar automáticamente el PDF en el expediente de Estados de Cuenta si está habilitado
    archivado_msg = ""
    key_pdf = f"{payload.cuenta_id}:{payload.archivo_nombre}"
    pdf_bytes = PENDING_PDF_UPLOADS.pop(key_pdf, None)
    if payload.archivar_pdf and pdf_bytes and payload.movimientos:
        primera_fecha = payload.movimientos[0].fecha
        anio_edo = primera_fecha.year
        mes_edo = primera_fecha.month
        ya_archivado = db.query(EstadoCuentaArchivo).filter(
            EstadoCuentaArchivo.cuenta_id == payload.cuenta_id,
            EstadoCuentaArchivo.anio == anio_edo,
            EstadoCuentaArchivo.mes == mes_edo,
            EstadoCuentaArchivo.nombre_archivo_original == payload.archivo_nombre
        ).first()
        if not ya_archivado:
            clean_filename = f"edo_{payload.cuenta_id}_{anio_edo}_{int(mes_edo):02d}_{secrets.token_hex(4)}.pdf"
            file_path = save_estado_cuenta_pdf(clean_filename, pdf_bytes, STORAGE_EDOS_DIR)
            nuevo_archivo = EstadoCuentaArchivo(
                cuenta_id=payload.cuenta_id,
                anio=anio_edo,
                mes=mes_edo,
                nombre_archivo_original=payload.archivo_nombre,
                nombre_archivo_almacenado=clean_filename,
                ruta_archivo=file_path,
                tamano_bytes=len(pdf_bytes),
                notas=f"Archivado automáticamente al importar {insertados} movimiento(s)."
            )
            db.add(nuevo_archivo)
            archivado_msg = f" El PDF fue archivado automáticamente en Estados de Cuenta ({NOMBRE_MESES_CORTO.get(mes_edo, mes_edo)} {anio_edo})."

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error al guardar los movimientos: {str(e)}")

    return {"message": f"✅ Proceso completado: {insertados} movimientos importados, {ignorados} omitidos por duplicidad.{archivado_msg}"}


# ====================================================================================================
# 5. REPORTES, BALANCES Y AUDITORÍA
# ====================================================================================================
@app.get("/api/reportes/resumen")
def reporte_financiero_global(
    cuenta_id: Optional[int] = None,
    categoria_id: Optional[int] = None,
    entidad_id: Optional[int] = None,
    tipo: Optional[str] = None,
    fecha_inicio: Optional[date] = None,
    fecha_fin: Optional[date] = None,
    request: Request = None,
    db: Session = Depends(get_db)
):
    query = db.query(Transaccion)
    alcance = _alcance_cuentas(request)
    if cuenta_id:
        _exigir_cuenta(request, cuenta_id)
        query = query.filter(Transaccion.cuenta_id == cuenta_id)
    elif alcance is not None:
        query = query.filter(Transaccion.cuenta_id.in_(alcance or [-1]))
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
    request: Request = None,
    db: Session = Depends(get_db)
):
    _exigir_cuenta(request, cuenta_id)
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

    cierre_obj = db.query(CierreMes).filter(
        CierreMes.cuenta_id == cuenta_id,
        CierreMes.anio == anio,
        CierreMes.mes == mes
    ).first()

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
        "cierre_mes": {
            "cerrado": bool(cierre_obj.cerrado) if cierre_obj else False,
            "saldo_banco_pdf": cierre_obj.saldo_banco_pdf if cierre_obj else None,
            "cerrado_por": cierre_obj.cerrado_por if cierre_obj else None
        },
        "movimientos": movimientos_formateados,
        "desglose_categorias": desglose_categorias
    }

@app.post("/api/reportes/corte-mes/conciliacion")
def guardar_conciliacion_cierre_mes(payload: ConciliacionCierrePayload, request: Request, db: Session = Depends(get_db)):
    _exigir_cuenta(request, payload.cuenta_id)
    if payload.cerrado is not None and _alcance_cuentas(request) is not None:
        raise HTTPException(status_code=403, detail="Solo el Administrador puede cerrar o reabrir un mes contable.")
    if _usuario_actual(request):
        payload.usuario = _usuario_actual(request)
    cierre = db.query(CierreMes).filter(
        CierreMes.cuenta_id == payload.cuenta_id,
        CierreMes.anio == payload.anio,
        CierreMes.mes == payload.mes
    ).first()
    if not cierre:
        cierre = CierreMes(
            cuenta_id=payload.cuenta_id,
            anio=payload.anio,
            mes=payload.mes,
            saldo_banco_pdf=payload.saldo_banco_pdf,
            cerrado=bool(payload.cerrado) if payload.cerrado is not None else False,
            cerrado_por=payload.usuario
        )
        db.add(cierre)
    else:
        if "saldo_banco_pdf" in payload.model_fields_set:
            cierre.saldo_banco_pdf = payload.saldo_banco_pdf
        if payload.cerrado is not None:
            cierre.cerrado = payload.cerrado
            cierre.cerrado_por = payload.usuario
    db.commit()
    db.refresh(cierre)
    return {
        "cerrado": bool(cierre.cerrado),
        "saldo_banco_pdf": cierre.saldo_banco_pdf,
        "cerrado_por": cierre.cerrado_por,
        "message": "🔒 Periodo cerrado y conciliado." if cierre.cerrado else "✅ Datos de conciliación actualizados."
    }

@app.get("/api/backup/download")
def descargar_respaldo_bd(db: Session = Depends(get_db)):
    import sqlite3
    import tempfile
    from sqlalchemy import create_engine as sa_create_engine
    from sqlalchemy.orm import sessionmaker as sa_sessionmaker
    from starlette.background import BackgroundTask

    hoy_str = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    backup_filename = f"Respaldo_Control_Bancos_{hoy_str}.db"
    temp_dir = tempfile.gettempdir()
    temp_path = os.path.join(temp_dir, f"backup_{secrets.token_hex(4)}_{backup_filename}")

    if IS_SQLITE:
        posibles_rutas = [
            os.path.join(os.path.dirname(__file__), "..", "hyma_fiscal.db"),
            os.path.join(os.path.dirname(__file__), "hyma_fiscal.db"),
            os.path.abspath("hyma_fiscal.db"),
            os.path.join(os.path.dirname(__file__), "..", "bancos.db"),
            os.path.join(os.path.dirname(__file__), "bancos.db"),
            os.path.abspath("bancos.db")
        ]
        db_path = None
        for cand in posibles_rutas:
            if os.path.exists(cand) and os.path.getsize(cand) > 0:
                db_path = os.path.abspath(cand)
                break

        if not db_path:
            raise HTTPException(status_code=404, detail="Archivo de base de datos no localizado.")

        try:
            src = sqlite3.connect(db_path)
            dst = sqlite3.connect(temp_path)
            with dst:
                src.backup(dst)
            dst.close()
            src.close()
            download_path = temp_path
            cleanup_needed = True
        except Exception:
            download_path = db_path
            cleanup_needed = False
    else:
        # En modo PostgreSQL (Nube), exporta todas las tablas a un archivo SQLite .db portable al vuelo
        tmp_engine = sa_create_engine(f"sqlite:///{temp_path}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=tmp_engine)
        TmpSession = sa_sessionmaker(autocommit=False, autoflush=False, bind=tmp_engine)
        tmp_db = TmpSession()
        try:
            for model_cls in [
                Configuracion, Usuario, Cuenta, Categoria, Entidad,
                ReglaClasificacion, Transaccion, CierreMes, EstadoCuentaArchivo
            ]:
                rows = db.query(model_cls).all()
                for row in rows:
                    data = {col.name: getattr(row, col.name) for col in model_cls.__table__.columns}
                    tmp_db.add(model_cls(**data))
                tmp_db.commit()
        finally:
            tmp_db.close()
            tmp_engine.dispose()
        download_path = temp_path
        cleanup_needed = True

    def cleanup():
        if cleanup_needed:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass

    return FileResponse(
        path=download_path,
        filename=backup_filename,
        media_type="application/octet-stream",
        background=BackgroundTask(cleanup) if cleanup_needed else None
    )

# ====================================================================================================
# ARCHIVOS DE ESTADOS DE CUENTA (PDFS OFICIALES POR CUENTA, AÑO Y MES)
# ====================================================================================================
STORAGE_EDOS_DIR = os.path.join(os.path.dirname(__file__), "storage", "estados_cuenta")
os.makedirs(STORAGE_EDOS_DIR, exist_ok=True)

@app.post("/api/archivos-estado-cuenta", response_model=EstadoCuentaArchivoResponse)
async def subir_archivo_estado_cuenta(
    request: Request,
    cuenta_id: int = Form(...),
    anio: int = Form(...),
    mes: int = Form(...),
    notas: Optional[str] = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    _exigir_cuenta(request, cuenta_id)
    cuenta = db.query(Cuenta).filter(Cuenta.id == cuenta_id).first()
    if not cuenta:
        raise HTTPException(status_code=404, detail="Cuenta bancaria no encontrada.")

    if not file.filename:
        raise HTTPException(status_code=400, detail="No se seleccionó ningún archivo.")

    ext = os.path.splitext(file.filename)[1].lower()
    if ext != ".pdf":
        raise HTTPException(status_code=400, detail="Únicamente se permiten archivos PDF del Estado de Cuenta Bancario.")

    clean_filename = f"edo_{cuenta_id}_{anio}_{int(mes):02d}_{secrets.token_hex(4)}.pdf"
    content = await file.read()
    file_path = save_estado_cuenta_pdf(clean_filename, content, STORAGE_EDOS_DIR)

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
    request: Request = None,
    db: Session = Depends(get_db)
):
    query = db.query(EstadoCuentaArchivo).join(Cuenta, EstadoCuentaArchivo.cuenta_id == Cuenta.id)
    alcance = _alcance_cuentas(request)
    if cuenta_id:
        _exigir_cuenta(request, cuenta_id)
        query = query.filter(EstadoCuentaArchivo.cuenta_id == cuenta_id)
    elif alcance is not None:
        query = query.filter(EstadoCuentaArchivo.cuenta_id.in_(alcance or [-1]))
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
def descargar_archivo_estado_cuenta(archivo_id: int, request: Request, db: Session = Depends(get_db)):
    archivo = db.query(EstadoCuentaArchivo).filter(EstadoCuentaArchivo.id == archivo_id).first()
    if not archivo:
        raise HTTPException(status_code=404, detail="Archivo de estado de cuenta no encontrado.")
    _exigir_cuenta(request, archivo.cuenta_id)

    resolved_path = resolve_or_fetch_estado_cuenta_pdf(
        archivo.nombre_archivo_almacenado, archivo.ruta_archivo, STORAGE_EDOS_DIR
    )
    if not resolved_path:
        raise HTTPException(status_code=404, detail="El archivo físico no se encuentra en el servidor ni en la bóveda en la nube.")

    return FileResponse(
        path=resolved_path,
        filename=archivo.nombre_archivo_original,
        media_type="application/pdf"
    )

@app.get("/api/archivos-estado-cuenta/{archivo_id}/ver")
def ver_archivo_estado_cuenta_inline(archivo_id: int, request: Request, db: Session = Depends(get_db)):
    archivo = db.query(EstadoCuentaArchivo).filter(EstadoCuentaArchivo.id == archivo_id).first()
    if not archivo:
        raise HTTPException(status_code=404, detail="Archivo de estado de cuenta no encontrado.")
    _exigir_cuenta(request, archivo.cuenta_id)

    resolved_path = resolve_or_fetch_estado_cuenta_pdf(
        archivo.nombre_archivo_almacenado, archivo.ruta_archivo, STORAGE_EDOS_DIR
    )
    if not resolved_path:
        raise HTTPException(status_code=404, detail="El archivo físico no se encuentra en el servidor ni en la bóveda en la nube.")

    return FileResponse(
        path=resolved_path,
        media_type="application/pdf"
    )

@app.delete("/api/archivos-estado-cuenta/{archivo_id}")
def eliminar_archivo_estado_cuenta(archivo_id: int, db: Session = Depends(get_db)):
    archivo = db.query(EstadoCuentaArchivo).filter(EstadoCuentaArchivo.id == archivo_id).first()
    if not archivo:
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")

    remove_estado_cuenta_pdf(archivo.nombre_archivo_almacenado, archivo.ruta_archivo, STORAGE_EDOS_DIR)

    db.delete(archivo)
    db.commit()
    return {"message": "Archivo eliminado con éxito."}

# Montar frontend estático
frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "static")
if os.path.exists(frontend_dir):
    app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

    @app.api_route("/", methods=["GET", "HEAD"])
    def index():
        response = FileResponse(os.path.join(frontend_dir, "index.html"))
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
