import re
import hashlib
from datetime import datetime, date
from decimal import Decimal
from typing import List, Dict, Any, Optional
import pdfplumber
import io

# Mapeo de meses en español para bancos mexicanos (ej. 15-ENE, 20-MAR)
MESES_ES = {
    "ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DIC": 12
}

def clean_amount(val_str: Optional[str]) -> Optional[Decimal]:
    """Limpia cadenas numéricas con comas, signos de pesos y espacios: '$ 1,250.50' -> Decimal('1250.50')"""
    if not val_str:
        return None
    cleaned = re.sub(r"[^\d.-]", "", str(val_str).strip())
    if not cleaned or cleaned == "-" or cleaned == ".":
        return None
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"))
    except Exception:
        return None

def parse_date(date_str: str, default_year: int = datetime.now().year) -> Optional[date]:
    """Detecta múltiples formatos de fecha en estados de cuenta bancarios mexicanos."""
    date_str = str(date_str).strip().upper()

    # 1. Formato DD-MMM-YY o DD-MMM-YYYY (ej. 01-OCT-26, 15-ENE-2026, 02-OCT-26TELCEL)
    match_banorte = re.search(r"(\d{1,2})-([A-Z]{3})-(\d{2,4})", date_str)
    if match_banorte:
        day = int(match_banorte.group(1))
        mes_key = match_banorte.group(2)
        year_val = match_banorte.group(3)
        year = int(year_val) if len(year_val) == 4 else 2000 + int(year_val)
        if mes_key in MESES_ES:
            return date(year, MESES_ES[mes_key], day)

    # 2. Formato DD/MM/YYYY o DD-MM-YYYY
    match_std = re.search(r"(\d{2})[/.-](\d{2})[/.-](\d{4})", date_str)
    if match_std:
        day, month, year = map(int, match_std.groups())
        return date(year, month, day)

    # 3. Formato DD/MM/YY
    match_short = re.search(r"(\d{2})[/.-](\d{2})[/.-](\d{2})", date_str)
    if match_short:
        day, month, year = map(int, match_short.groups())
        return date(2000 + year, month, day)

    # 4. Formato DD MMM (ej: '15 ENE', '24 FEB')
    match_month_name = re.search(r"(\d{1,2})[\s/-]([A-Z]{3})", date_str)
    if match_month_name:
        day = int(match_month_name.group(1))
        mes_key = match_month_name.group(2)
        if mes_key in MESES_ES:
            return date(default_year, MESES_ES[mes_key], day)

    return None

def generate_movement_hash(cuenta_id: int, f_date: date, monto: Decimal, tipo: str, concepto: str) -> str:
    """Genera una huella digital SHA-256 única para evitar duplicados en la base de datos."""
    normalized_concept = re.sub(r"\s+", " ", concepto.strip().upper())
    raw = f"{cuenta_id}_{f_date.isoformat()}_{monto:.2f}_{tipo}_{normalized_concept}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def _extract_banorte_words(page, cuenta_id: int, existing_hashes: set) -> List[Dict[str, Any]]:
    """
    Extractor de alta precisión para estados de cuenta Banorte (Enlace Negocios / PyME).
    Usa detección posicional de columnas DEPOSITO, RETIRO y SALDO y agrupa palabras verticalmente por fecha.
    """
    movements = []
    words = page.extract_words()
    if not words:
        return []

    # Localizar encabezados en la sección de movimientos
    dep_header = next((w for w in words if w['text'] == 'DEPOSITO' and w['top'] < 120), None)
    ret_header = next((w for w in words if w['text'] == 'RETIRO' and w['top'] < 120), None)
    sal_header = next((w for w in words if w['text'] == 'SALDO' and w['top'] < 120), None)

    # Coordenadas horizontales predeterminadas para Banorte si no se detectan
    col_deposito = (dep_header['x0'] - 40, dep_header['x1'] + 30) if dep_header else (340.0, 445.0)
    col_retiro = (ret_header['x0'] - 20, ret_header['x1'] + 30) if ret_header else (445.0, 520.0)
    col_saldo = (sal_header['x0'] - 20, sal_header['x1'] + 60) if sal_header else (520.0, 610.0)

    # Localizar filas de movimientos identificadas por fecha DD-MMM-YY
    date_regex = re.compile(r"^(\d{2})-([A-Z]{3})-(\d{2})")
    date_entries = []

    for w in words:
        m = date_regex.match(w['text'])
        # Filtrar fechas dentro del área de detalle de movimientos
        if m and (85 < w['top'] < 520):
            day = int(m.group(1))
            mes_str = m.group(2)
            year = 2000 + int(m.group(3))
            mes = MESES_ES.get(mes_str, 1)
            mov_date = date(year, mes, day)
            date_entries.append({
                "date": mov_date,
                "top": w['top'],
                "bottom": w['bottom'],
                "first_word": w['text']
            })

    if not date_entries:
        return []

    for i, de in enumerate(date_entries):
        top_bound = de['top'] - 2
        bottom_bound = date_entries[i + 1]['top'] - 2 if i + 1 < len(date_entries) else (de['top'] + 60)

        row_words = [w for w in words if top_bound <= w['top'] < bottom_bound]

        # Extraer concepto (palabras a la izquierda de la columna de depósito)
        concept_words = [w['text'] for w in row_words if w['x1'] <= (col_deposito[0] + 15)]
        concept_raw = " ".join(concept_words)

        # Limpiar prefijo de fecha pegado al concepto (ej. '01-OCT-264385584CARGO' -> '4385584 CARGO')
        concept_clean = re.sub(r"^\d{2}-[A-Z]{3}-\d{2}", "", concept_raw).strip()
        concept_clean = re.sub(r"\s+", " ", concept_clean)

        # Omitir el saldo anterior (balance inicial del mes)
        if "SALDO ANTERIOR" in concept_clean.upper():
            continue

        deposito_amt = None
        retiro_amt = None
        saldo_amt = None

        for w in row_words:
            # Buscar montos monetarios con punto decimal
            if "." in w['text']:
                amt = clean_amount(w['text'])
                if amt is not None:
                    x_mid = (w['x0'] + w['x1']) / 2
                    if col_deposito[0] <= x_mid <= col_deposito[1]:
                        deposito_amt = amt
                    elif col_retiro[0] <= x_mid <= col_retiro[1]:
                        retiro_amt = amt
                    elif col_saldo[0] <= x_mid <= col_saldo[1]:
                        saldo_amt = amt

        tipo = "ENTRADA" if (deposito_amt is not None and deposito_amt > 0) else "SALIDA"
        monto = deposito_amt if tipo == "ENTRADA" else retiro_amt

        if monto is None:
            continue

        # Extraer posible folio o referencia numérica
        ref_match = re.search(r"(?:REF|REFERENCIA|CVE\.?RASTREO|AUTORIZA)[:\s]*([A-Z0-9]+)", concept_clean, re.IGNORECASE)
        referencia = ref_match.group(1) if ref_match else ""

        m_hash = generate_movement_hash(cuenta_id, de['date'], monto, tipo, concept_clean)

        movements.append({
            "fecha": de['date'],
            "concepto": concept_clean[:250],
            "referencia": referencia,
            "cargo": monto if tipo == "SALIDA" else None,
            "abono": monto if tipo == "ENTRADA" else None,
            "tipo": tipo,
            "monto": monto,
            "saldo_resultante": saldo_amt,
            "hash_deduplicacion": m_hash,
            "es_duplicado": m_hash in existing_hashes
        })

    return movements


def extract_movements_from_pdf(
    file_bytes: bytes, 
    cuenta_id: int, 
    existing_hashes: set
) -> List[Dict[str, Any]]:
    """
    Motor híbrido inteligente de extracción multibanco (Banorte, Scotiabank, BBVA, Santander, etc.):
    1) Si detecta formato Banorte (Enlace Negocios / Detalle de Movimientos), usa extracción posicional.
    2) Si detecta tablas estructuradas estándar, extrae por celdas tabulares.
    3) Si es texto libre, aplica heurística de expresiones regulares multibanco.
    """
    movements: List[Dict[str, Any]] = []
    current_year = datetime.now().year

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        # A) PROBAR ESTRATEGIA 1: BANORTE / POSICIONAL
        for page in pdf.pages:
            text = page.extract_text() or ""
            if "DETALLE DE MOVIMIENTOS" in text and ("BANORTE" in text.upper() or "ENLACE NEGOCIOS" in text.upper()):
                banorte_movs = _extract_banorte_words(page, cuenta_id, existing_hashes)
                if banorte_movs:
                    movements.extend(banorte_movs)

        if movements:
            return movements

        # B) ESTRATEGIA 2: TABLAS NATIVAS (Scotiabank, BBVA, etc.)
        for page in pdf.pages:
            tables = page.extract_tables()
            table_extracted = False

            for table in tables:
                if not table or len(table) < 2:
                    continue

                header_row = None
                for row_idx, row in enumerate(table[:5]):
                    row_txt = " ".join([str(c or "").upper() for c in row])
                    if any(k in row_txt for k in ["FECHA", "CONCEPTO", "DESCRIPCIÓN", "CARGO", "ABONO", "RETIRO", "DEPÓSITO"]):
                        header_row = row_idx
                        break

                if header_row is not None:
                    headers = [str(c or "").upper().strip() for c in table[header_row]]
                    idx_fecha = next((i for i, h in enumerate(headers) if "FECHA" in h or "DIA" in h), 0)
                    idx_concepto = next((i for i, h in enumerate(headers) if any(k in h for k in ["CONCEPTO", "DESCRIPCION", "DETALLE"])), 1)
                    idx_cargo = next((i for i, h in enumerate(headers) if any(k in h for k in ["CARGO", "RETIRO", "SALIDA", "DEBITO"])), None)
                    idx_abono = next((i for i, h in enumerate(headers) if any(k in h for k in ["ABONO", "DEPOSITO", "ENTRADA", "CREDITO"])), None)
                    idx_saldo = next((i for i, h in enumerate(headers) if "SALDO" in h), None)

                    for row in table[header_row + 1:]:
                        max_idx = max(filter(lambda x: x is not None, [idx_fecha, idx_concepto, idx_cargo, idx_abono]))
                        if len(row) <= max_idx:
                            continue

                        f_raw = row[idx_fecha]
                        c_raw = row[idx_concepto]
                        if not f_raw or not c_raw:
                            continue

                        m_date = parse_date(str(f_raw), current_year)
                        if not m_date:
                            continue

                        cargo_amt = clean_amount(row[idx_cargo]) if idx_cargo is not None and row[idx_cargo] else None
                        abono_amt = clean_amount(row[idx_abono]) if idx_abono is not None and row[idx_abono] else None
                        saldo_amt = clean_amount(row[idx_saldo]) if idx_saldo is not None and row[idx_saldo] else None

                        if not cargo_amt and not abono_amt:
                            continue

                        tipo = "SALIDA" if cargo_amt and cargo_amt > 0 else "ENTRADA"
                        monto = cargo_amt if tipo == "SALIDA" else abono_amt
                        concepto_str = re.sub(r"\s+", " ", str(c_raw)).strip()

                        if "SALDO ANTERIOR" in concepto_str.upper():
                            continue

                        m_hash = generate_movement_hash(cuenta_id, m_date, monto, tipo, concepto_str)

                        movements.append({
                            "fecha": m_date,
                            "concepto": concepto_str,
                            "referencia": "",
                            "cargo": cargo_amt,
                            "abono": abono_amt,
                            "tipo": tipo,
                            "monto": monto,
                            "saldo_resultante": saldo_amt,
                            "hash_deduplicacion": m_hash,
                            "es_duplicado": m_hash in existing_hashes
                        })
                        table_extracted = True

            # C) ESTRATEGIA 3: REGEX LÍNEA POR LÍNEA MULTIBANCO
            if not table_extracted:
                text = page.extract_text()
                if not text:
                    continue

                line_regex = re.compile(
                    r"^(\d{2}[/-]\d{2}(?:[/-]\d{2,4})?|\d{1,2}\s+[A-Z]{3}|\d{1,2}-[A-Z]{3}-\d{2,4})\s+(.+?)\s+([\d,]+\.\d{2})(?:\s+([\d,]+\.\d{2}))?",
                    re.IGNORECASE | re.MULTILINE
                )

                for line in text.split("\n"):
                    match = line_regex.search(line.strip())
                    if match:
                        f_str, concepto_str, amt1_str, amt2_str = match.groups()
                        m_date = parse_date(f_str, current_year)
                        if not m_date:
                            continue

                        amt1 = clean_amount(amt1_str)
                        amt2 = clean_amount(amt2_str) if amt2_str else None

                        if not amt1:
                            continue

                        con_upper = concepto_str.upper()
                        if "SALDO ANTERIOR" in con_upper:
                            continue

                        es_salida = any(w in con_upper for w in ["RETIRO", "CARGO", "COMPRA", "COMISION", "PAGO", "TRANSFERENCIA SPEI ENVIADA", "SPEI A "])
                        es_entrada = any(w in con_upper for w in ["ABONO", "DEPOSITO", "DEPOSITO EN EFECTIVO", "SPEI RECIBIDO", "NOMINA"])

                        if es_entrada:
                            tipo = "ENTRADA"
                        elif es_salida:
                            tipo = "SALIDA"
                        else:
                            tipo = "SALIDA"

                        monto = amt1
                        m_hash = generate_movement_hash(cuenta_id, m_date, monto, tipo, concepto_str)

                        movements.append({
                            "fecha": m_date,
                            "concepto": concepto_str.strip(),
                            "referencia": "",
                            "cargo": monto if tipo == "SALIDA" else None,
                            "abono": monto if tipo == "ENTRADA" else None,
                            "tipo": tipo,
                            "monto": monto,
                            "saldo_resultante": amt2,
                            "hash_deduplicacion": m_hash,
                            "es_duplicado": m_hash in existing_hashes
                        })

    return movements
