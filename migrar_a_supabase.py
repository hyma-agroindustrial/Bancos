"""
Script Maestro de Migración y Auditoría Contable (Turbo Bulk + Parallel Storage):
SQLite Local (hyma_fiscal.db + 64 PDFs + Logos) ---> Supabase (PostgreSQL + Storage)
Ejecución: python -u migrar_a_supabase.py
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from dotenv import load_dotenv
from sqlalchemy import create_engine, func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import sessionmaker

from backend.models import (
    Base, Configuracion, Usuario, Cuenta, Categoria, Entidad,
    ReglaClasificacion, Transaccion, CierreMes, EstadoCuentaArchivo
)
from backend.cloud_storage import (
    is_cloud_storage_enabled, get_supabase_config, ensure_buckets_exist,
    upload_bytes_to_supabase
)

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SQLITE_PATH = os.path.join(BASE_DIR, "hyma_fiscal.db")
EDOS_DIR = os.path.join(BASE_DIR, "backend", "storage", "estados_cuenta")
UPLOADS_DIR = os.path.join(BASE_DIR, "frontend", "static", "uploads")


def get_pg_url() -> str:
    raw = (os.getenv("DATABASE_URL") or "").strip()
    if not raw or raw.startswith("sqlite"):
        print("❌ ERROR: Debes configurar DATABASE_URL con tu cadena PostgreSQL de Supabase en el archivo .env", flush=True)
        sys.exit(1)
    if raw.startswith("postgres://"):
        return raw.replace("postgres://", "postgresql+psycopg2://", 1)
    if raw.startswith("postgresql://"):
        return raw.replace("postgresql://", "postgresql+psycopg2://", 1)
    return raw


def bulk_upsert(dst_db, model_cls, rows, pk_col="id", batch_size=250):
    if not rows:
        return
    table = model_cls.__table__
    cols = [c.name for c in table.columns]
    dict_rows = [{col: getattr(r, col) for col in cols} for r in rows]

    for i in range(0, len(dict_rows), batch_size):
        chunk = dict_rows[i:i + batch_size]
        stmt = pg_insert(table).values(chunk)
        update_cols = {c: getattr(stmt.excluded, c) for c in cols if c != pk_col}
        stmt = stmt.on_conflict_do_update(index_elements=[pk_col], set_=update_cols)
        dst_db.execute(stmt)
    dst_db.commit()


def run_migration():
    print("=" * 78, flush=True)
    print("🚀 INICIANDO MIGRACIÓN MAESTRA A SUPABASE (POSTGRESQL + STORAGE)", flush=True)
    print("=" * 78, flush=True)

    if not os.path.exists(SQLITE_PATH):
        print(f"❌ No se encontró la base de datos local en: {SQLITE_PATH}", flush=True)
        sys.exit(1)

    pg_url = get_pg_url()
    sqlite_engine = create_engine(f"sqlite:///{SQLITE_PATH}", connect_args={"check_same_thread": False})
    pg_engine = create_engine(pg_url, pool_pre_ping=True)

    SqliteSession = sessionmaker(bind=sqlite_engine)
    PgSession = sessionmaker(bind=pg_engine)

    src_db = SqliteSession()
    dst_db = PgSession()

    try:
        # 1. Crear tablas en PostgreSQL
        print("\n[1/5] Creando esquema de 8 tablas relacionales en Supabase PostgreSQL...", flush=True)
        Base.metadata.create_all(bind=pg_engine)
        print("  ✅ Esquema verificado en PostgreSQL.", flush=True)

        # 2. Migrar tablas en orden jerárquico estricto con Bulk Upsert
        ordered_models = [
            ("configuracion", Configuracion, "clave"),
            ("usuarios", Usuario, "id"),
            ("cuentas", Cuenta, "id"),
            ("categorias", Categoria, "id"),
            ("entidades", Entidad, "id"),
            ("reglas_clasificacion", ReglaClasificacion, "id"),
            ("transacciones", Transaccion, "id"),
            ("cierres_mes", CierreMes, "id"),
            ("estados_cuenta_archivos", EstadoCuentaArchivo, "id"),
        ]

        print("\n[2/5] Copiando registros contables por lotes (SQLite -> PostgreSQL)...", flush=True)
        for label, model_cls, pk in ordered_models:
            rows = src_db.query(model_cls).all()
            bulk_upsert(dst_db, model_cls, rows, pk_col=pk)
            pg_cnt = dst_db.query(model_cls).count()
            print(f"  ✅ Tabla '{label}': {len(rows)} locales -> {pg_cnt} en Supabase PostgreSQL", flush=True)

        # 3. Sincronizar secuencias SERIAL/IDENTITY en PostgreSQL
        print("\n[3/5] Sincronizando secuencias de IDs (autoincrement) en PostgreSQL...", flush=True)
        tables_with_id = [
            "usuarios", "cuentas", "categorias", "entidades",
            "reglas_clasificacion", "transacciones", "cierres_mes", "estados_cuenta_archivos"
        ]
        for tbl in tables_with_id:
            sql_seq = text(
                f"SELECT setval(pg_get_serial_sequence('{tbl}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {tbl}), 1), "
                f"(SELECT COUNT(*) > 0 FROM {tbl}));"
            )
            dst_db.execute(sql_seq)
        dst_db.commit()
        print("  ✅ Todas las secuencias de IDs sincronizadas sin riesgo de colisión.", flush=True)

        # 4. Subir PDFs y Logos a Supabase Storage en paralelo
        print("\n[4/5] Subiendo archivos PDF y Logos a Supabase Storage...", flush=True)
        if not is_cloud_storage_enabled():
            print("  ⚠️ SUPABASE_URL o SUPABASE_SERVICE_ROLE_KEY no están en .env; se omitió subida de archivos.", flush=True)
        else:
            ensure_buckets_exist()
            cfg = get_supabase_config()

            pdf_files = sorted([f for f in os.listdir(EDOS_DIR) if f.lower().endswith(".pdf")]) if os.path.exists(EDOS_DIR) else []

            def _upload_one_pdf(fname: str) -> bool:
                fpath = os.path.join(EDOS_DIR, fname)
                with open(fpath, "rb") as fh:
                    content = fh.read()
                return upload_bytes_to_supabase(cfg["bucket_pdfs"], fname, content, "application/pdf")

            uploaded_pdfs = 0
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = {pool.submit(_upload_one_pdf, f): f for f in pdf_files}
                for idx, fut in enumerate(as_completed(futures), 1):
                    if fut.result():
                        uploaded_pdfs += 1
                    if idx % 16 == 0 or idx == len(pdf_files):
                        print(f"  📄 PDFs subidos a Supabase Storage: {uploaded_pdfs}/{len(pdf_files)}...", flush=True)

            # Subir Logos de branding
            if os.path.exists(UPLOADS_DIR):
                for fname in os.listdir(UPLOADS_DIR):
                    fpath = os.path.join(UPLOADS_DIR, fname)
                    if not os.path.isfile(fpath) or fname.startswith("."):
                        continue
                    ext = os.path.splitext(fname)[1].lower()
                    mime = "image/png" if ext == ".png" else "image/jpeg"
                    with open(fpath, "rb") as fh:
                        content = fh.read()
                    if upload_bytes_to_supabase(cfg["bucket_branding"], fname, content, mime):
                        pub_url = f"{cfg['url']}/storage/v1/object/public/{cfg['bucket_branding']}/{fname}"
                        logo_cfg = dst_db.query(Configuracion).filter(Configuracion.clave == "logo_url").first()
                        if logo_cfg and fname in (logo_cfg.valor or ""):
                            logo_cfg.valor = pub_url
                            dst_db.commit()
                        print(f"  🖼️ Logo subido: {fname} -> {pub_url}", flush=True)

        # 5. Cuadre Contable Automático (SQLite Local vs PostgreSQL Nube)
        print("\n[5/5] AUDITORÍA DE CUADRE CONTABLE (SQLITE LOCAL vs SUPABASE NUBE):", flush=True)
        print("-" * 78, flush=True)
        print(f"{'ID':<4} {'Cuenta Bancaria':<26} {'Saldo SQLite':>18} {'Saldo Supabase':>18} {'Estado':>8}", flush=True)
        print("-" * 78, flush=True)

        cuentas_local = src_db.query(Cuenta).order_by(Cuenta.id).all()
        todo_cuadrado = True
        for c in cuentas_local:
            ent_loc = src_db.query(func.coalesce(func.sum(Transaccion.monto), 0)).filter(
                Transaccion.cuenta_id == c.id, Transaccion.tipo == "ENTRADA"
            ).scalar()
            sal_loc = src_db.query(func.coalesce(func.sum(Transaccion.monto), 0)).filter(
                Transaccion.cuenta_id == c.id, Transaccion.tipo == "SALIDA"
            ).scalar()
            saldo_loc = Decimal(str(c.saldo_inicial)) + Decimal(str(ent_loc)) - Decimal(str(sal_loc))

            c_pg = dst_db.query(Cuenta).filter(Cuenta.id == c.id).first()
            ent_pg = dst_db.query(func.coalesce(func.sum(Transaccion.monto), 0)).filter(
                Transaccion.cuenta_id == c.id, Transaccion.tipo == "ENTRADA"
            ).scalar()
            sal_pg = dst_db.query(func.coalesce(func.sum(Transaccion.monto), 0)).filter(
                Transaccion.cuenta_id == c.id, Transaccion.tipo == "SALIDA"
            ).scalar()
            saldo_pg = Decimal(str(c_pg.saldo_inicial)) + Decimal(str(ent_pg)) - Decimal(str(sal_pg))

            ok = (saldo_loc == saldo_pg)
            if not ok:
                todo_cuadrado = False
            status_str = "✅ OK" if ok else "❌ DIFF"
            print(f"{c.id:<4} {c.nombre[:25]:<26} ${saldo_loc:>16,.2f} ${saldo_pg:>16,.2f} {status_str:>8}", flush=True)

        print("-" * 78, flush=True)
        if todo_cuadrado:
            print("🏆 AUDITORÍA EXITOSA: Las 9 cuentas cuadran al 100% al centavo en la nube.", flush=True)
        else:
            print("⚠️ ADVERTENCIA: Se detectó diferencia en alguna cuenta.", flush=True)

    finally:
        src_db.close()
        dst_db.close()
        sqlite_engine.dispose()
        pg_engine.dispose()


if __name__ == "__main__":
    run_migration()
