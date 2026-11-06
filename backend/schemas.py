from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
from datetime import date, datetime
from decimal import Decimal

# --- CUENTAS ---
class CuentaBase(BaseModel):
    nombre: str = Field(..., max_length=100, examples=["Fiscal Principal"])
    banco: str = Field(..., max_length=80, examples=["BBVA Bancomer"])
    clabe: str = Field(..., min_length=18, max_length=18, examples=["012180001234567890"])
    numero_cuenta: str = Field(..., max_length=25, examples=["0123456789"])
    saldo_inicial: Decimal = Field(default=Decimal("0.00"), ge=0)
    moneda: str = Field(default="MXN", max_length=5)

    @field_validator("clabe")
    @classmethod
    def validar_clabe(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit() or len(v) != 18:
            raise ValueError("La CLABE interbancaria debe tener exactamente 18 dígitos numéricos.")
        return v

class CuentaCreate(CuentaBase):
    pass

class CuentaResponse(CuentaBase):
    id: int
    activo: bool
    saldo_actual: Optional[Decimal] = Decimal("0.00")
    total_entradas: Optional[Decimal] = Decimal("0.00")
    total_salidas: Optional[Decimal] = Decimal("0.00")
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True

# --- CATEGORIAS ---
class CategoriaBase(BaseModel):
    nombre: str = Field(..., max_length=100)
    tipo: str = Field(..., pattern="^(ENTRADA|SALIDA|AMBOS)$")
    tipo_entidad_sugerida: str = Field(default="GENERAL")
    descripcion: Optional[str] = None

class CategoriaCreate(CategoriaBase):
    pass

class CategoriaResponse(CategoriaBase):
    id: int
    activo: bool

    class Config:
        from_attributes = True

# --- ENTIDADES ---
class EntidadBase(BaseModel):
    nombre: str = Field(..., max_length=150)
    tipo_entidad: str = Field(..., examples=["CLIENTE", "GASOLINERA", "PROVEEDOR", "EMPLEADO", "SAT", "GENERAL"])
    rfc: Optional[str] = Field(None, max_length=13)
    telefono: Optional[str] = None
    email: Optional[str] = None

class EntidadCreate(EntidadBase):
    pass

class EntidadResponse(EntidadBase):
    id: int
    activo: bool
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True

# --- TRANSACCIONES ---
class TransaccionBase(BaseModel):
    cuenta_id: int
    categoria_id: Optional[int] = None
    entidad_id: Optional[int] = None
    fecha: date
    tipo: str = Field(..., pattern="^(ENTRADA|SALIDA)$")
    monto: Decimal = Field(..., gt=0)
    concepto: str
    referencia: Optional[str] = None

class TransaccionCreate(TransaccionBase):
    origen: Optional[str] = "MANUAL"
    archivo_fuente: Optional[str] = None
    hash_deduplicacion: Optional[str] = None

class TransaccionResponse(TransaccionBase):
    id: int
    origen: str
    archivo_fuente: Optional[str]
    cuenta_nombre: Optional[str] = None
    categoria_nombre: Optional[str] = None
    entidad_nombre: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True

# --- PDF PREVIEW / BATCH ---
class MovimientoExtraido(BaseModel):
    fecha: date
    concepto: str
    referencia: Optional[str] = None
    cargo: Optional[Decimal] = None # Salida
    abono: Optional[Decimal] = None  # Entrada
    tipo: str
    monto: Decimal
    saldo_resultante: Optional[Decimal] = None
    hash_deduplicacion: str
    es_duplicado: bool = False
    categoria_id_sugerida: Optional[int] = None
    entidad_id_sugerida: Optional[int] = None

class BatchImportConfirm(BaseModel):
    cuenta_id: int
    archivo_nombre: str
    movimientos: List[MovimientoExtraido]

