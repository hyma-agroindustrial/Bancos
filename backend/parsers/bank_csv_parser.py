import csv
import io
import re
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from typing import List, Dict, Any, Optional, Tuple

from .bank_pdf_parser import (
    MESES_ES,
    parse_date,
    generate_movement_hash,
    _disambiguate_intra_pdf_hashes,
)

NOMBRE_MESES_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
    5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"
}


def clean_csv_decimal(val_str: Optional[Any]) -> Optional[Decimal]:
    """
    Convierte una cadena monetaria de CSV bancario a Decimal('0.01') exacto.
    Soporta:
      - Signos de moneda y espacios: '$ 1,250.50', 'MXN 1,250.50'
      - Negativos con signo o paréntesis contables: '-1,250.50', '(1,250.50)'
      - Formato europeo si aplica (1.250,50)
    Nunca utiliza float.
    """
    if val_str is None:
        return None
    raw = str(val_str).strip()
    if not raw or raw in ("-", "--", "N/A", "NULL", "NONE", "$", "$ -", "$-"):
        return None

    is_negative = False
    if (raw.startswith("(") and raw.endswith(")")) or raw.startswith("-") or raw.endswith("-"):
        is_negative = True

    # Detectar si usa coma como separador decimal (ej. 1.234,56)
    if re.search(r"\d+\.\d{3},\d{2}$", raw) or ("," in raw and "." not in raw and re.search(r",\d{2}$", raw)):
        raw = raw.replace(".", "").replace(",", ".")

    cleaned = re.sub(r"[^\d.]", "", raw)
    if not cleaned or cleaned == ".":
        return None

    try:
        dec = Decimal(cleaned).quantize(Decimal("0.01"))
        return -dec if is_negative else dec
    except (InvalidOperation, ValueError):
        return None


def parse_csv_date(val_str: Optional[Any], default_year: int) -> Optional[date]:
    """
    Reconoce fechas en múltiples formatos de CSV bancarios:
      - YYYY-MM-DD / YYYY/MM/DD (ISO)
      - DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY
      - DD/MM/YY, DD-MM-YY
      - DD-MMM-YY, DD-MMM-YYYY, DD/MMM/YYYY (español o inglés)
      - Timestamps con hora: '2026-10-15 14:23:00', '15/10/2026 09:15'
    """
    if val_str is None:
        return None
    s = str(val_str).strip().upper()
    if not s:
        return None

    # Mapear meses en inglés comunes en exportaciones CSV bancarias a español
    en_to_es = {
        "JAN": "ENE", "APR": "ABR", "AUG": "AGO", "DEC": "DIC"
    }
    for en_k, es_v in en_to_es.items():
        s = re.sub(rf"\b{en_k}\b", es_v, s)

    # 1. Formato ISO YYYY-MM-DD o YYYY/MM/DD
    m_iso = re.match(r"^(\d{4})[/.-](\d{1,2})[/.-](\d{1,2})", s)
    if m_iso:
        yr, mo, dy = map(int, m_iso.groups())
        try:
            return date(yr, mo, dy)
        except ValueError:
            pass

    # 2. Delegar al parser de fechas multibanco existente
    dt = parse_date(s, default_year=default_year)
    if dt:
        return dt

    # 3. Formato D/M/YYYY o D-M-YYYY con 1 solo dígito en día o mes
    m_dmy = re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", s)
    if m_dmy:
        dy, mo, yr_raw = int(m_dmy.group(1)), int(m_dmy.group(2)), m_dmy.group(3)
        yr = int(yr_raw) if len(yr_raw) == 4 else (2000 + int(yr_raw))
        try:
            return date(yr, mo, dy)
        except ValueError:
            pass

    return None


def _decode_csv_bytes(file_bytes: bytes) -> str:
    """Decodifica bytes de CSV probando UTF-8-SIG, UTF-8, CP1252 y Latin-1."""
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return file_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return file_bytes.decode("latin-1", errors="ignore")


def _detect_delimiter_and_rows(text: str) -> List[List[str]]:
    """Detecta el delimitador óptimo (coma, punto y coma, tabulador o pipe) y parsea las filas respetando comillas."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []

    candidates = (",", ";", "\t", "|")
    best_delim = ","
    best_score = -1

    for d in candidates:
        try:
            parsed = [row for row in csv.reader(io.StringIO("\n".join(lines[:30])), delimiter=d) if any(c.strip() for c in row)]
            # Puntuar por cantidad de filas que tienen >= 3 columnas
            multi_col_rows = [len(r) for r in parsed if len(r) >= 3]
            score = len(multi_col_rows) * 10 + (max(multi_col_rows) if multi_col_rows else 0)
            if score > best_score:
                best_score = score
                best_delim = d
        except Exception:
            continue

    reader = csv.reader(io.StringIO(text), delimiter=best_delim)
    return [row for row in reader if any(str(c).strip() for c in row)]


def _extract_preheader_metadata(rows: List[List[str]]) -> Dict[str, Any]:
    """
    Escanea las primeras filas de metadatos (antes de la tabla de movimientos)
    para detectar año, fecha de reporte/corte, saldo inicial declarado, moneda y cuenta.
    """
    meta: Dict[str, Any] = {
        "year": datetime.now().year,
        "fecha_reporte": None,
        "saldo_inicial_declarado": None,
        "saldo_final_declarado": None,
        "moneda_detectada": None,
        "cuenta_detectada": None,
    }

    for row in rows[:20]:
        joined = " ".join(str(c).strip() for c in row if str(c).strip())
        joined_up = joined.upper()

        # Detectar año explícito en encabezado
        m_yr = re.search(r"\b(20\d{2})\b", joined_up)
        if m_yr:
            yr = int(m_yr.group(1))
            if 2000 <= yr <= 2100:
                meta["year"] = yr

        # Detectar fecha de reporte / corte / emisión
        if any(k in joined_up for k in ("FECHA DE CORTE", "CORTE", "FECHA REPORTE", "PERIODO", "AL ")):
            for cell in row:
                dt = parse_csv_date(cell, meta["year"])
                if dt:
                    meta["fecha_reporte"] = dt
                    meta["year"] = dt.year

        # Detectar saldo inicial declarado en encabezado (soporta miles con coma aún sin comillas)
        if any(k in joined_up for k in ("SALDO INICIAL", "SALDO ANTERIOR", "BALANCE INICIAL")):
            m_si = re.search(
                r"(?:SALDO\s*(?:INICIAL|ANTERIOR)|BALANCE\s*INICIAL)[^0-9$-]{0,25}(-?\$?\s*\d[\d,\s]*\.\d{2})",
                joined_up
            )
            if m_si:
                amt = clean_csv_decimal(m_si.group(1).replace(" ", ""))
                if amt is not None:
                    meta["saldo_inicial_declarado"] = amt
            else:
                for cell in reversed(row):
                    amt = clean_csv_decimal(cell)
                    if amt is not None:
                        meta["saldo_inicial_declarado"] = amt
                        break

        # Detectar saldo final declarado en encabezado
        if any(k in joined_up for k in ("SALDO FINAL", "SALDO ACTUAL", "SALDO AL CORTE")):
            m_sf = re.search(
                r"(?:SALDO\s*(?:FINAL|ACTUAL|AL\s*CORTE))[^0-9$-]{0,25}(-?\$?\s*\d[\d,\s]*\.\d{2})",
                joined_up
            )
            if m_sf:
                amt = clean_csv_decimal(m_sf.group(1).replace(" ", ""))
                if amt is not None:
                    meta["saldo_final_declarado"] = amt
            else:
                for cell in reversed(row):
                    amt = clean_csv_decimal(cell)
                    if amt is not None:
                        meta["saldo_final_declarado"] = amt
                        break

        # Detectar moneda
        if re.search(r"\b(USD|DOLARES|DÓLARES)\b", joined_up):
            meta["moneda_detectada"] = "USD"
        elif re.search(r"\b(MXN|MN|PESOS)\b", joined_up) and not meta["moneda_detectada"]:
            meta["moneda_detectada"] = "MXN"

        # Detectar CLABE (18 dígitos) o cuenta (10-11 dígitos)
        m_clabe = re.search(r"\b(\d{18})\b", joined_up)
        if m_clabe:
            meta["cuenta_detectada"] = m_clabe.group(1)
        elif not meta["cuenta_detectada"]:
            m_cta = re.search(r"(?:CUENTA|CTA|NO\.?)[:\s#]*(\d{7,12})\b", joined_up)
            if m_cta:
                meta["cuenta_detectada"] = m_cta.group(1)

    return meta


def build_period_summary(
    movements: List[Dict[str, Any]],
    formato_origen: str = "CSV",
    saldo_inicial_declarado: Optional[Decimal] = None,
    saldo_final_declarado: Optional[Decimal] = None,
    fecha_reporte_declarada: Optional[date] = None,
    moneda_detectada: Optional[str] = None,
    cuenta_detectada: Optional[str] = None,
    advertencias: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Calcula el resumen contable del periodo con precisión Decimal('0.01'):
      - Detecta automáticamente fecha_inicio, fecha_fin, fecha_reporte, mes y año del reporte.
      - Deduce el Saldo Inicial si no fue declarado explícitamente usando el primer movimiento con saldo_resultante.
      - Suma Total Cargos y Total Abonos.
      - Calcula Saldo Final = Saldo Inicial + Total Abonos - Total Cargos.
      - Verifica cuadre exacto contra el último saldo_resultante del estado de cuenta.
    """
    adv = list(advertencias or [])
    if not movements:
        hoy = fecha_reporte_declarada or date.today()
        z = Decimal("0.00")
        return {
            "formato_origen": formato_origen,
            "fecha_reporte": hoy,
            "fecha_inicio": None,
            "fecha_fin": None,
            "mes_detectado": hoy.month,
            "anio_detectado": hoy.year,
            "periodo_etiqueta": f"{NOMBRE_MESES_ES.get(hoy.month, '')} {hoy.year}",
            "saldo_inicial": saldo_inicial_declarado or z,
            "total_abonos": z,
            "total_cargos": z,
            "conteo_abonos": 0,
            "conteo_cargos": 0,
            "total_movimientos": 0,
            "saldo_final_calculado": saldo_inicial_declarado or z,
            "saldo_final_archivo": saldo_final_declarado,
            "diferencia_cuadre": z,
            "cuadra_exacto": True,
            "moneda_detectada": moneda_detectada,
            "cuenta_detectada": cuenta_detectada,
            "advertencias": adv,
        }

    fechas = [m["fecha"] for m in movements if isinstance(m.get("fecha"), date)]
    fecha_inicio = min(fechas) if fechas else date.today()
    fecha_fin = max(fechas) if fechas else date.today()
    fecha_reporte = fecha_reporte_declarada or fecha_fin

    # Determinar el mes/año predominante del estado de cuenta (moda de meses para evitar que un día de corte del mes previo cambie el periodo)
    month_counts: Dict[Tuple[int, int], int] = {}
    for f in fechas:
        key = (f.year, f.month)
        month_counts[key] = month_counts.get(key, 0) + 1
    anio_det, mes_det = max(month_counts, key=month_counts.get) if month_counts else (fecha_fin.year, fecha_fin.month)

    total_abonos = Decimal("0.00")
    total_cargos = Decimal("0.00")
    conteo_abonos = 0
    conteo_cargos = 0

    for m in movements:
        monto = Decimal(str(m["monto"])).quantize(Decimal("0.01"))
        if m["tipo"] == "ENTRADA":
            total_abonos += monto
            conteo_abonos += 1
        else:
            total_cargos += monto
            conteo_cargos += 1

    # Deducir Saldo Inicial si no se declaró explícitamente y el primer movimiento tiene saldo_resultante
    saldo_inicial = saldo_inicial_declarado
    first_with_bal = next((m for m in movements if m.get("saldo_resultante") is not None), None)
    if saldo_inicial is None and first_with_bal is not None:
        idx_first = movements.index(first_with_bal)
        bal_after_first = Decimal(str(first_with_bal["saldo_resultante"])).quantize(Decimal("0.01"))
        # Revertir los movimientos desde el 0 hasta idx_first inclusive
        net_until_first = Decimal("0.00")
        for m in movements[: idx_first + 1]:
            amt = Decimal(str(m["monto"])).quantize(Decimal("0.01"))
            net_until_first += amt if m["tipo"] == "ENTRADA" else -amt
        saldo_inicial = (bal_after_first - net_until_first).quantize(Decimal("0.01"))

    if saldo_inicial is None:
        saldo_inicial = Decimal("0.00")

    saldo_final_calculado = (saldo_inicial + total_abonos - total_cargos).quantize(Decimal("0.01"))

    # Saldo final reportado en el archivo (declarado o el último saldo_resultante disponible)
    saldo_final_archivo = saldo_final_declarado
    if saldo_final_archivo is None:
        last_with_bal = next((m for m in reversed(movements) if m.get("saldo_resultante") is not None), None)
        if last_with_bal is not None:
            saldo_final_archivo = Decimal(str(last_with_bal["saldo_resultante"])).quantize(Decimal("0.01"))

    if saldo_final_archivo is not None:
        diferencia_cuadre = (saldo_final_calculado - saldo_final_archivo).quantize(Decimal("0.01"))
        cuadra_exacto = abs(diferencia_cuadre) <= Decimal("0.02")
        if not cuadra_exacto:
            adv.append(
                f"Se detectó una diferencia de ${diferencia_cuadre:,.2f} entre el saldo final calculado "
                f"(${saldo_final_calculado:,.2f}) y el saldo final reportado por el banco (${saldo_final_archivo:,.2f})."
            )
    else:
        diferencia_cuadre = Decimal("0.00")
        cuadra_exacto = True

    periodo_etiqueta = (
        f"{NOMBRE_MESES_ES.get(mes_det, '')} {anio_det} "
        f"({fecha_inicio.strftime('%d/%m/%Y')} al {fecha_fin.strftime('%d/%m/%Y')})"
    )

    return {
        "formato_origen": formato_origen,
        "fecha_reporte": fecha_reporte,
        "fecha_inicio": fecha_inicio,
        "fecha_fin": fecha_fin,
        "mes_detectado": mes_det,
        "anio_detectado": anio_det,
        "periodo_etiqueta": periodo_etiqueta,
        "saldo_inicial": saldo_inicial,
        "total_abonos": total_abonos,
        "total_cargos": total_cargos,
        "conteo_abonos": conteo_abonos,
        "conteo_cargos": conteo_cargos,
        "total_movimientos": len(movements),
        "saldo_final_calculado": saldo_final_calculado,
        "saldo_final_archivo": saldo_final_archivo,
        "diferencia_cuadre": diferencia_cuadre,
        "cuadra_exacto": cuadra_exacto,
        "moneda_detectada": moneda_detectada,
        "cuenta_detectada": cuenta_detectada,
        "advertencias": adv,
    }


def extract_movements_from_csv(
    file_bytes: bytes,
    cuenta_id: int,
    existing_hashes: set,
    existing_signatures: Optional[set] = None,
    cuenta_obj: Optional[Any] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Motor universal de importación de estados de cuenta bancarios en formato CSV.
    Soporta exportaciones de Scotiabank, Banorte, BBVA, Santander, Banamex, BanBajío, HSBC y formatos contables estándar.
    Devuelve: (lista_movimientos, resumen_periodo)
    """
    text = _decode_csv_bytes(file_bytes)
    rows = _detect_delimiter_and_rows(text)
    if not rows:
        return [], build_period_summary([], formato_origen="CSV")

    meta = _extract_preheader_metadata(rows)
    default_year = meta["year"]
    advertencias: List[str] = []

    # Validar correspondencia con la cuenta bancaria activa si el CSV trae número de cuenta o moneda
    if cuenta_obj is not None:
        if meta["moneda_detectada"] and getattr(cuenta_obj, "moneda", None):
            cta_mon = str(cuenta_obj.moneda).strip().upper()
            if meta["moneda_detectada"] != cta_mon:
                advertencias.append(
                    f"⚠️ El archivo CSV indica moneda {meta['moneda_detectada']}, mientras que la cuenta activa está configurada en {cta_mon}."
                )
        if meta["cuenta_detectada"]:
            det = meta["cuenta_detectada"]
            num_cta = re.sub(r"\D", "", str(getattr(cuenta_obj, "numero_cuenta", "") or ""))
            clabe_cta = re.sub(r"\D", "", str(getattr(cuenta_obj, "clabe", "") or ""))
            if len(det) >= 4 and num_cta and not (det[-4:] == num_cta[-4:] or (clabe_cta and det[-4:] in clabe_cta)):
                advertencias.append(
                    f"⚠️ El archivo CSV menciona la cuenta *{det[-4:]}, que no coincide con la terminación de la cuenta activa (*{num_cta[-4:]})."
                )

    # Localizar la fila de encabezados de la tabla de movimientos
    header_idx: Optional[int] = None
    col_map: Dict[str, Optional[int]] = {
        "fecha": None,
        "concepto": None,
        "referencia": None,
        "cargo": None,
        "abono": None,
        "monto": None,
        "tipo": None,
        "saldo": None,
    }

    for idx, row in enumerate(rows[:35]):
        norm_cells = [re.sub(r"\s+", " ", str(c).strip().upper()) for c in row]
        has_fecha = any(any(k in c for k in ("FECHA", "DATE", "DIA", "DÍA", "F. OPER")) for c in norm_cells)
        has_amt = any(
            any(k in c for k in ("CARGO", "ABONO", "RETIRO", "DEPOSITO", "DEPÓSITO", "IMPORTE", "MONTO", "SALIDA", "ENTRADA", "DEBITO", "CREDITO", "AMOUNT"))
            for c in norm_cells
        )
        if has_fecha and has_amt:
            header_idx = idx
            for c_i, col_name in enumerate(norm_cells):
                if col_map["fecha"] is None and any(k in col_name for k in ("FECHA", "DATE", "DIA", "DÍA")):
                    col_map["fecha"] = c_i
                elif col_map["concepto"] is None and any(k in col_name for k in ("CONCEPTO", "DESCRIP", "DETALLE", "MOVIMIENTO", "NARRATIVA", "OBSERV")):
                    col_map["concepto"] = c_i
                elif col_map["referencia"] is None and any(k in col_name for k in ("REFERENCIA", "FOLIO", "RASTREO", "AUTORIZA", "REF", "CHEQUE", "CODIGO", "CÓDIGO")):
                    col_map["referencia"] = c_i
                elif col_map["cargo"] is None and any(k in col_name for k in ("CARGO", "RETIRO", "SALIDA", "DEBITO", "DÉBITO", "EGRESO")):
                    col_map["cargo"] = c_i
                elif col_map["abono"] is None and any(k in col_name for k in ("ABONO", "DEPOSITO", "DEPÓSITO", "ENTRADA", "CREDITO", "CRÉDITO", "INGRESO")):
                    col_map["abono"] = c_i
                elif col_map["saldo"] is None and any(k in col_name for k in ("SALDO", "BALANCE", "DISPONIBLE")):
                    col_map["saldo"] = c_i
                elif col_map["tipo"] is None and any(k in col_name for k in ("TIPO", "NATURALEZA", "C/A", "SENTIDO")):
                    col_map["tipo"] = c_i
                elif col_map["monto"] is None and any(k in col_name for k in ("IMPORTE", "MONTO", "VALOR", "AMOUNT", "CANTIDAD")):
                    col_map["monto"] = c_i
            break

    start_row = (header_idx + 1) if header_idx is not None else 0
    raw_extracted: List[Dict[str, Any]] = []
    saldo_inicial_fila: Optional[Decimal] = meta["saldo_inicial_declarado"]

    for r_idx, row in enumerate(rows[start_row:], start=start_row):
        cells = [str(c).strip() for c in row]
        joined_up = " ".join(cells).upper()

        if not any(cells):
            continue

        # Detectar fila de Saldo Anterior / Inicial dentro de la tabla
        if any(k in joined_up for k in ("SALDO ANTERIOR", "SALDO INICIAL", "BALANCE INICIAL")):
            if col_map["saldo"] is not None and col_map["saldo"] < len(cells):
                amt = clean_csv_decimal(cells[col_map["saldo"]])
                if amt is not None:
                    saldo_inicial_fila = amt
            else:
                for c_val in reversed(cells):
                    amt = clean_csv_decimal(c_val)
                    if amt is not None:
                        saldo_inicial_fila = amt
                        break
            continue

        # Omitir filas de totales o encabezados repetidos
        if any(joined_up.startswith(k) for k in ("TOTAL", "SUBTOTAL", "RESUMEN", "FECHA,CONCEPTO")):
            continue

        # 1. Extraer Fecha
        f_date: Optional[date] = None
        if col_map["fecha"] is not None and col_map["fecha"] < len(cells):
            f_date = parse_csv_date(cells[col_map["fecha"]], default_year)
        if f_date is None:
            # Buscar fecha en las primeras 3 columnas
            for c_val in cells[:3]:
                f_date = parse_csv_date(c_val, default_year)
                if f_date:
                    break
        if f_date is None:
            continue

        # 2. Extraer Concepto y Referencia
        concepto = ""
        if col_map["concepto"] is not None and col_map["concepto"] < len(cells):
            concepto = cells[col_map["concepto"]]
        referencia = ""
        if col_map["referencia"] is not None and col_map["referencia"] < len(cells):
            referencia = cells[col_map["referencia"]]

        if not concepto:
            # Construir concepto de columnas de texto que no sean fecha ni montos
            text_parts = []
            used_indices = {i for i in col_map.values() if i is not None}
            for c_i, c_val in enumerate(cells):
                if c_i in used_indices:
                    continue
                if parse_csv_date(c_val, default_year) is None and clean_csv_decimal(c_val) is None:
                    text_parts.append(c_val)
            concepto = " ".join(text_parts).strip() or (referencia or "MOVIMIENTO BANCARIO CSV")

        concepto_clean = re.sub(r"\s+", " ", concepto).strip()
        referencia_clean = re.sub(r"\s+", " ", referencia).strip()

        if not referencia_clean:
            ref_match = re.search(
                r"(?:REF\.?|REFERENCIA|CVE\.?\s*RASTREO|FOLIO|AUT\.?)[:\s#]*([A-Z0-9-]{4,35})",
                concepto_clean,
                re.IGNORECASE,
            )
            if ref_match:
                referencia_clean = ref_match.group(1)

        # 3. Extraer Cargo / Abono / Saldo con precisión Decimal('0.01')
        cargo_amt: Optional[Decimal] = None
        abono_amt: Optional[Decimal] = None
        saldo_amt: Optional[Decimal] = None

        if col_map["saldo"] is not None and col_map["saldo"] < len(cells):
            saldo_amt = clean_csv_decimal(cells[col_map["saldo"]])

        if col_map["cargo"] is not None or col_map["abono"] is not None:
            if col_map["cargo"] is not None and col_map["cargo"] < len(cells):
                c_val = clean_csv_decimal(cells[col_map["cargo"]])
                if c_val is not None and abs(c_val) > Decimal("0.00"):
                    cargo_amt = abs(c_val)
            if col_map["abono"] is not None and col_map["abono"] < len(cells):
                a_val = clean_csv_decimal(cells[col_map["abono"]])
                if a_val is not None and abs(a_val) > Decimal("0.00"):
                    abono_amt = abs(a_val)

        # Si viene en una sola columna de Importe/Monto
        if cargo_amt is None and abono_amt is None and col_map["monto"] is not None and col_map["monto"] < len(cells):
            m_val = clean_csv_decimal(cells[col_map["monto"]])
            if m_val is not None and abs(m_val) > Decimal("0.00"):
                tipo_cell = cells[col_map["tipo"]].upper() if (col_map["tipo"] is not None and col_map["tipo"] < len(cells)) else ""
                if any(k in tipo_cell for k in ("ABONO", "DEPOSITO", "DEPÓSITO", "ENTRADA", "CRED", "+")) or (
                    not tipo_cell and m_val > Decimal("0.00") and not any(k in concepto_clean.upper() for k in ("CARGO", "RETIRO", "COMISION", "PAGO "))
                ):
                    if m_val < Decimal("0.00"):
                        cargo_amt = abs(m_val)
                    else:
                        abono_amt = abs(m_val)
                else:
                    if m_val < Decimal("0.00") or any(k in tipo_cell for k in ("CARGO", "RETIRO", "SALIDA", "DEB", "-")):
                        cargo_amt = abs(m_val)
                    else:
                        abono_amt = abs(m_val)

        if (cargo_amt is None or cargo_amt == Decimal("0.00")) and (abono_amt is None or abono_amt == Decimal("0.00")):
            continue

        if abono_amt is not None and abono_amt > Decimal("0.00") and (cargo_amt is None or cargo_amt == Decimal("0.00")):
            tipo = "ENTRADA"
            monto = abono_amt
            cargo_amt = None
        else:
            tipo = "SALIDA"
            monto = cargo_amt or Decimal("0.00")
            abono_amt = None

        raw_extracted.append({
            "row_order": r_idx,
            "fecha": f_date,
            "concepto": concepto_clean[:250],
            "referencia": referencia_clean[:100],
            "cargo": cargo_amt,
            "abono": abono_amt,
            "tipo": tipo,
            "monto": monto,
            "saldo_resultante": saldo_amt,
        })

    # 4. Detectar si el CSV viene en orden cronológico inverso (del más reciente al más antiguo) y normalizar a ascendente
    if len(raw_extracted) >= 2:
        first_d = raw_extracted[0]["fecha"]
        last_d = raw_extracted[-1]["fecha"]
        if first_d > last_d:
            raw_extracted.reverse()
        elif first_d == last_d and raw_extracted[0].get("saldo_resultante") is not None and raw_extracted[1].get("saldo_resultante") is not None:
            # Verificar dirección por progresión aritmética de saldos
            b0 = raw_extracted[0]["saldo_resultante"]
            b1 = raw_extracted[1]["saldo_resultante"]
            m0 = raw_extracted[0]["monto"] if raw_extracted[0]["tipo"] == "ENTRADA" else -raw_extracted[0]["monto"]
            if abs((b1 + m0) - b0) <= Decimal("0.02"):
                raw_extracted.reverse()

    # 5. Generar hash idempotente (compatible con PDF y con verificación de firmas existentes)
    sig_set = existing_signatures or set()
    movements: List[Dict[str, Any]] = []
    for item in raw_extracted:
        ref_str = item["referencia"]
        concept_str = item["concepto"]
        hash_concept = f"{concept_str} {ref_str}".strip() if (ref_str and ref_str not in concept_str) else concept_str
        m_hash = generate_movement_hash(cuenta_id, item["fecha"], item["monto"], item["tipo"], hash_concept)
        alt_hash = generate_movement_hash(cuenta_id, item["fecha"], item["monto"], item["tipo"], concept_str)

        # Firma canónica cruzada (fecha, monto, tipo, referencia_limpia) para detectar duplicados entre PDF y CSV
        ref_canon = re.sub(r"\W+", "", ref_str.upper())
        conc_canon = re.sub(r"\W+", "", concept_str.upper())[:24]
        cross_sig_ref = (item["fecha"].isoformat(), f"{item['monto']:.2f}", item["tipo"], ref_canon) if len(ref_canon) >= 4 else None
        cross_sig_conc = (item["fecha"].isoformat(), f"{item['monto']:.2f}", item["tipo"], conc_canon)

        is_dup = (
            (m_hash in existing_hashes)
            or (alt_hash in existing_hashes)
            or (cross_sig_ref is not None and cross_sig_ref in sig_set)
            or (cross_sig_conc in sig_set)
        )
        if alt_hash in existing_hashes and m_hash not in existing_hashes:
            m_hash = alt_hash

        movements.append({
            "fecha": item["fecha"],
            "concepto": concept_str,
            "referencia": ref_str,
            "cargo": item["cargo"],
            "abono": item["abono"],
            "tipo": item["tipo"],
            "monto": item["monto"],
            "saldo_resultante": item["saldo_resultante"],
            "hash_deduplicacion": m_hash,
            "es_duplicado": is_dup,
        })

    movements = _disambiguate_intra_pdf_hashes(movements, cuenta_id, existing_hashes)

    summary = build_period_summary(
        movements=movements,
        formato_origen="CSV",
        saldo_inicial_declarado=saldo_inicial_fila,
        saldo_final_declarado=meta["saldo_final_declarado"],
        fecha_reporte_declarada=meta["fecha_reporte"],
        moneda_detectada=meta["moneda_detectada"],
        cuenta_detectada=meta["cuenta_detectada"],
        advertencias=advertencias,
    )
    return movements, summary
