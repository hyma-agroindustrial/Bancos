from sqlalchemy import Column, Integer, String, Numeric, Boolean, Date, DateTime, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.orm import relationship
from .database import Base

class Configuracion(Base):
    __tablename__ = "configuracion"

    clave = Column(String(50), primary_key=True)
    valor = Column(Text, nullable=False)

class Usuario(Base):
    __tablename__ = "usuarios"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    nombre = Column(String(100), nullable=False)
    pin_hash = Column(String(128), nullable=False)
    rol = Column(String(20), default="ADMIN", nullable=False) # 'ADMIN', 'CAPTURISTA'
    activo = Column(Boolean, default=True, nullable=False)
    token_sesion = Column(String(64), nullable=True, index=True)
    created_at = Column(DateTime, server_default=func.now())

class Cuenta(Base):
    __tablename__ = "cuentas"

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(100), nullable=False)
    banco = Column(String(80), nullable=False)
    clabe = Column(String(18), unique=True, nullable=False, index=True)
    numero_cuenta = Column(String(25), unique=True, nullable=False, index=True)
    saldo_inicial = Column(Numeric(15, 2), default=0.00, nullable=False)
    moneda = Column(String(5), default="MXN", nullable=False)
    activo = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    transacciones = relationship("Transaccion", back_populates="cuenta", cascade="all, delete-orphan")

class Categoria(Base):
    __tablename__ = "categorias"

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(100), unique=True, nullable=False)
    tipo = Column(String(10), nullable=False) # 'ENTRADA', 'SALIDA', 'AMBOS'
    tipo_entidad_sugerida = Column(String(30), default="GENERAL", nullable=False)
    descripcion = Column(Text, nullable=True)
    activo = Column(Boolean, default=True, nullable=False)

    transacciones = relationship("Transaccion", back_populates="categoria")

class Entidad(Base):
    __tablename__ = "entidades"

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(150), nullable=False, index=True)
    tipo_entidad = Column(String(30), nullable=False, index=True) # 'CLIENTE', 'GASOLINERA', 'PROVEEDOR', 'EMPLEADO', 'SAT', 'GENERAL'
    rfc = Column(String(13), nullable=True)
    telefono = Column(String(20), nullable=True)
    email = Column(String(100), nullable=True)
    activo = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    transacciones = relationship("Transaccion", back_populates="entidad")

    __table_args__ = (
        UniqueConstraint("nombre", "tipo_entidad", name="uq_entidad_nombre_tipo"),
    )

class Transaccion(Base):
    __tablename__ = "transacciones"

    id = Column(Integer, primary_key=True, index=True)
    cuenta_id = Column(Integer, ForeignKey("cuentas.id", ondelete="CASCADE"), nullable=False, index=True)
    categoria_id = Column(Integer, ForeignKey("categorias.id", ondelete="SET NULL"), nullable=True, index=True)
    entidad_id = Column(Integer, ForeignKey("entidades.id", ondelete="SET NULL"), nullable=True, index=True)
    
    fecha = Column(Date, nullable=False, index=True)
    tipo = Column(String(10), nullable=False) # 'ENTRADA' o 'SALIDA'
    monto = Column(Numeric(15, 2), nullable=False)
    concepto = Column(Text, nullable=False)
    referencia = Column(String(100), nullable=True)
    
    origen = Column(String(15), default="MANUAL", nullable=False) # 'MANUAL' o 'PDF_EXTRACT'
    archivo_fuente = Column(String(255), nullable=True)
    hash_deduplicacion = Column(String(64), unique=True, nullable=True, index=True)
    created_at = Column(DateTime, server_default=func.now())

    cuenta = relationship("Cuenta", back_populates="transacciones")
    categoria = relationship("Categoria", back_populates="transacciones")
    entidad = relationship("Entidad", back_populates="transacciones")
