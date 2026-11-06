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
    cleaned = re.sub(r"[^\d.-]", "", val_str.strip())
    if not cleaned or cleaned == "-" or cleaned == ".":
        return None
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"))
    except Exception:
        return None

def parse_date(date_str: str, default_year: int = datetime.now().year) -> Optional[date]:
    """Detecta múltiples formatos de fecha en estados de cuenta bancarios."""
    date_str = date_str.strip().upper()
    
    # 1. Formato DD/MM/YYYY o DD-MM-YYYY
    match_std = re.search(r"(\d{2})[/.-](\d{2})[/.-](\d{4})", date_str)
    if match_std:
        day, month, year = map(int, match_std.groups())
        return date(year, month, day)

    # 2. Formato DD/MM/YY
    match_short = re.search(r"(\d{2})[/.-](\d{2})[/.-](\d{2})", date_str)
    if match_short:
        day, month, year = map(int, match_short.groups())
        return date(2000 + year, month, day)

    # 3. Formato DD-MMM o DD/MMM (ej: '15 ENE', '24-FEB')
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

def extract_movements_from_pdf(
    file_bytes: bytes, 
    cuenta_id: int, 
    existing_hashes: set
) -> List[Dict[str, Any]]:
    """
    Motor híbrido de extracción:
    1) Analiza tablas estructuradas detectadas por pdfplumber.
    2) Si el banco usa texto libre con columnas espaciadas, aplica expresiones regulares bancarias.
    """
    movements: List[Dict[str, Any]] = []
    current_year = datetime.now().year

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page_num, page in enumerate(pdf.pages):
            # A) INTENTO 1: Extracción por Tablas nativas
            tables = page.extract_tables()
            table_extracted = False
            
            for table in tables:
                if not table or len(table) < 2:
                    continue
                
                # Buscar encabezados de interés
                header_row = None
                for row_idx, row in enumerate(table[:5]):
                    row_txt = " ".join([str(c or "").upper() for c in row])
                    if any(k in row_txt for k in ["FECHA", "CONCEPTO", "DESCRIPCIÓN", "CARGO", "ABONO", "RETIRO", "DEPÓSITO"]):
                        header_row = row_idx
                        break
                
                if header_row is not None:
                    # Mapear columnas dinámicamente
                    headers = [str(c or "").upper().strip() for c in table[header_row]]
                    
                    idx_fecha = next((i for i, h in enumerate(headers) if "FECHA" in h or "DIA" in h), 0)
                    idx_concepto = next((i for i, h in enumerate(headers) if any(k in h for k in ["CONCEPTO", "DESCRIPCION", "DETALLE"])), 1)
                    idx_cargo = next((i for i, h in enumerate(headers) if any(k in h for k in ["CARGO", "RETIRO", "SALIDA", "DEBITO"])), None)
                    idx_abono = next((i for i, h in enumerate(headers) if any(k in h for k in ["ABONO", "DEPOSITO", "ENTRADA", "CREDITO"])), None)
                    idx_saldo = next((i for i, h in enumerate(headers) if "SALDO" in h), None)

                    for row in table[header_row + 1:]:
                        if len(row) <= max(filter(lambda x: x is not None, [idx_fecha, idx_concepto, idx_cargo, idx_abono])):
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

            # B) INTENTO 2: Si no hubo tablas claras, parsear líneas de texto con Regex Multibanco
            if not table_extracted:
                text = page.extract_text()
                if not text:
                    continue
                
                # Patrón estándar bancario:
                # [FECHA DD/MM/YYYY o DD-MMM] [DESCRIPCIÓN TEXTUAL] [CANTIDAD MONTO CARGO O ABONO] ...
                line_regex = re.compile(
                    r"^(\d{2}[/-]\d{2}(?:[/-]\d{2,4})?|\d{1,2}\s+[A-Z]{3})\s+(.+?)\s+([\d,]+\.\d{2})(?:\s+([\d,]+\.\d{2}))?",
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
                        
                        # Heurística: si hay dos montos, el segundo suele ser el saldo o abono
                        # Detectar palabras clave en el concepto para inferir tipo
                        con_upper = concepto_str.upper()
                        es_salida = any(w in con_upper for w in ["RETIRO", "CARGO", "COMPRA", "COMISION", "PAGO", "TRANSFERENCIA SPEI ENVIADA", "SPEI A "])
                        es_entrada = any(w in con_upper for w in ["ABONO", "DEPOSITO", "DEPOSITO EN EFECTIVO", "SPEI RECIBIDO", "NOMINA"])
                        
                        if es_entrada:
                            tipo = "ENTRADA"
                        elif es_salida:
                            tipo = "SALIDA"
                        else:
                            # Por defecto si no se sabe y solo hay una columna de monto, clasificar según posición o saldo
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

