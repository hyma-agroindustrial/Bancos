import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# Cargar variables de entorno desde archivo .env si existe
load_dotenv()

raw_db_url = (os.getenv("DATABASE_URL") or "").strip()
if not raw_db_url:
    raw_db_url = "sqlite:///./hyma_fiscal.db"

# Normalizar esquema postgres:// o postgresql:// para SQLAlchemy 2.0 + psycopg2
if raw_db_url.startswith("postgres://"):
    DATABASE_URL = raw_db_url.replace("postgres://", "postgresql+psycopg2://", 1)
elif raw_db_url.startswith("postgresql://"):
    DATABASE_URL = raw_db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
else:
    DATABASE_URL = raw_db_url

IS_SQLITE = DATABASE_URL.startswith("sqlite")

# Si se usa SQLite, se requiere check_same_thread=False
connect_args = {"check_same_thread": False} if IS_SQLITE else {}

engine_kwargs = {
    "connect_args": connect_args,
    "pool_pre_ping": True,
}
if not IS_SQLITE:
    engine_kwargs["pool_recycle"] = 300
    engine_kwargs["pool_size"] = 5
    engine_kwargs["max_overflow"] = 10

engine = create_engine(DATABASE_URL, **engine_kwargs)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
