import os
import json
import urllib.request
import urllib.error
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

def get_supabase_config() -> dict:
    return {
        "url": (os.getenv("SUPABASE_URL") or "").strip().rstrip("/"),
        "key": (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip(),
        "bucket_pdfs": (os.getenv("SUPABASE_BUCKET_PDFS") or "estados-cuenta").strip(),
        "bucket_branding": (os.getenv("SUPABASE_BUCKET_BRANDING") or "branding").strip(),
    }

def is_cloud_storage_enabled() -> bool:
    cfg = get_supabase_config()
    return bool(cfg["url"] and cfg["key"])

def _supabase_headers(cfg: dict, content_type: str = "application/json", extra: Optional[dict] = None) -> dict:
    headers = {
        "apikey": cfg["key"],
        "Authorization": f"Bearer {cfg['key']}",
        "Content-Type": content_type,
    }
    if extra:
        headers.update(extra)
    return headers

def ensure_buckets_exist() -> None:
    """Crea automáticamente los buckets en Supabase Storage si aún no existen."""
    if not is_cloud_storage_enabled():
        return
    cfg = get_supabase_config()
    buckets_to_ensure = [
        {"id": cfg["bucket_pdfs"], "name": cfg["bucket_pdfs"], "public": False},
        {"id": cfg["bucket_branding"], "name": cfg["bucket_branding"], "public": True},
    ]
    for b in buckets_to_ensure:
        try:
            req = urllib.request.Request(
                url=f"{cfg['url']}/storage/v1/bucket",
                data=json.dumps(b).encode("utf-8"),
                headers=_supabase_headers(cfg, "application/json"),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as _:
                pass
        except urllib.error.HTTPError as e:
            # 409 / Duplicate significa que el bucket ya existe
            if e.code not in (400, 409):
                pass
        except Exception:
            pass

def upload_bytes_to_supabase(bucket: str, object_name: str, content: bytes, content_type: str) -> bool:
    if not is_cloud_storage_enabled():
        return False
    cfg = get_supabase_config()
    url = f"{cfg['url']}/storage/v1/object/{bucket}/{object_name}"
    headers = _supabase_headers(cfg, content_type, {"x-upsert": "true"})
    try:
        req = urllib.request.Request(url=url, data=content, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status in (200, 201)
    except Exception:
        return False

def download_bytes_from_supabase(bucket: str, object_name: str) -> Optional[bytes]:
    if not is_cloud_storage_enabled():
        return None
    cfg = get_supabase_config()
    url = f"{cfg['url']}/storage/v1/object/{bucket}/{object_name}"
    headers = {
        "apikey": cfg["key"],
        "Authorization": f"Bearer {cfg['key']}",
    }
    try:
        req = urllib.request.Request(url=url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 200:
                return resp.read()
    except Exception:
        return None
    return None

def delete_object_from_supabase(bucket: str, object_name: str) -> bool:
    if not is_cloud_storage_enabled():
        return False
    cfg = get_supabase_config()
    url = f"{cfg['url']}/storage/v1/object/{bucket}/{object_name}"
    headers = {
        "apikey": cfg["key"],
        "Authorization": f"Bearer {cfg['key']}",
    }
    try:
        req = urllib.request.Request(url=url, headers=headers, method="DELETE")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status in (200, 204)
    except Exception:
        return False

def save_estado_cuenta_pdf(clean_filename: str, content: bytes, local_dir: str) -> str:
    """
    Guarda el PDF en el directorio local (caché/respaldo) y, si Supabase Storage
    está habilitado, lo sube también al bucket en la nube.
    """
    os.makedirs(local_dir, exist_ok=True)
    local_path = os.path.join(local_dir, clean_filename)
    with open(local_path, "wb") as f:
        f.write(content)

    if is_cloud_storage_enabled():
        cfg = get_supabase_config()
        ctype = "text/csv; charset=utf-8" if clean_filename.lower().endswith(".csv") else "application/pdf"
        upload_bytes_to_supabase(cfg["bucket_pdfs"], clean_filename, content, ctype)

    return local_path

def resolve_or_fetch_estado_cuenta_pdf(nombre_almacenado: str, ruta_registrada: str, local_dir: str) -> Optional[str]:
    """
    Localiza el archivo PDF en disco local (resolviendo rutas cruzadas Windows/Linux).
    Si el contenedor en la nube se reinició y no está en disco local, lo descarga
    automáticamente desde Supabase Storage y lo guarda en caché local.
    """
    os.makedirs(local_dir, exist_ok=True)
    candidates = []
    if nombre_almacenado:
        candidates.append(os.path.join(local_dir, os.path.basename(nombre_almacenado.replace("\\", "/"))))
    if ruta_registrada:
        candidates.append(ruta_registrada)
        basename_from_win = os.path.basename(ruta_registrada.replace("\\", "/"))
        if basename_from_win:
            candidates.append(os.path.join(local_dir, basename_from_win))

    for cand in candidates:
        if cand and os.path.exists(cand) and os.path.getsize(cand) > 0:
            return cand

    # Si no existe en disco local, recuperar desde Supabase Storage
    target_name = os.path.basename((nombre_almacenado or ruta_registrada or "").replace("\\", "/"))
    if target_name and is_cloud_storage_enabled():
        cfg = get_supabase_config()
        pdf_bytes = download_bytes_from_supabase(cfg["bucket_pdfs"], target_name)
        if pdf_bytes:
            cached_path = os.path.join(local_dir, target_name)
            with open(cached_path, "wb") as f:
                f.write(pdf_bytes)
            return cached_path

    return None

def remove_estado_cuenta_pdf(nombre_almacenado: str, ruta_registrada: str, local_dir: str) -> None:
    target_name = os.path.basename((nombre_almacenado or ruta_registrada or "").replace("\\", "/"))
    for p in [ruta_registrada, os.path.join(local_dir, target_name) if target_name else None]:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass
    if target_name and is_cloud_storage_enabled():
        cfg = get_supabase_config()
        delete_object_from_supabase(cfg["bucket_pdfs"], target_name)

def save_branding_image(clean_filename: str, content: bytes, ext: str, local_uploads_dir: str) -> str:
    """
    Guarda el logotipo en disco local y, si Supabase Storage está activo,
    lo sube al bucket público 'branding' devolviendo su URL pública permanente.
    """
    os.makedirs(local_uploads_dir, exist_ok=True)
    local_path = os.path.join(local_uploads_dir, clean_filename)
    with open(local_path, "wb") as f:
        f.write(content)

    mime_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".gif": "image/gif",
    }
    content_type = mime_map.get(ext.lower(), "application/octet-stream")

    if is_cloud_storage_enabled():
        cfg = get_supabase_config()
        ok = upload_bytes_to_supabase(cfg["bucket_branding"], clean_filename, content, content_type)
        if ok:
            return f"{cfg['url']}/storage/v1/object/public/{cfg['bucket_branding']}/{clean_filename}"

    return f"/static/uploads/{clean_filename}"
