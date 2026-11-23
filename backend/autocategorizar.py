"""Sugerencias de categoría para movimientos sin categoría.

Aprende del historial ya clasificado por el propio usuario. Una "pista" es una huella del movimiento:
  - clabe : cuenta destino de 18 dígitos dentro del concepto (SPEI / traspaso a terceros)
  - rfc   : RFC del comercio (pagos con tarjeta)
  - bnet  : cuenta destino en "PAGO CUENTA DE TERCERO BNET"
  - kw    : palabra clave (envases, cartón, etiquetas, honorarios, fletes, extractos)
Nunca modifica datos: solo propone. Aplicar se hace con el mismo endpoint de edición masiva.
"""
import re
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

_RFC = re.compile(r"RFC:\s*([A-ZÑ&]{3,4})\s*(\d{6}[A-Z0-9]{3})")
_CLABE = re.compile(r"(?<!\d)0{0,2}(\d{18})(?!\d)")
_BNET = re.compile(r"BNET\s+(\d{6,12})")
_PALABRAS = (
    ("ENVASE", "envases"), ("CARTON", "carton"), ("CARTÓN", "carton"), ("ETIQUETA", "etiquetas"),
    ("HONORARIOS", "honorarios"), ("FLET", "fletes"), ("GUIAS", "fletes"), ("EXTR", "extractos"),
)
_PRIORIDAD = {"clabe": 0, "rfc": 1, "bnet": 2, "kw": 3}
_TIPO_ETIQUETA = {"clabe": "Cuenta destino", "rfc": "Comercio (RFC)", "bnet": "Cuenta destino", "kw": "Palabra clave"}


def pistas(concepto: Optional[str]) -> List[Tuple[str, str]]:
    c = concepto or ""
    t = c.upper()
    out: List[Tuple[str, str]] = []
    m = _RFC.search(t)
    if m:
        out.append(("rfc", m.group(1) + m.group(2)))
    for m in _CLABE.finditer(c):
        out.append(("clabe", m.group(1)))
    m = _BNET.search(c)
    if m:
        out.append(("bnet", m.group(1)))
    for palabra, clave in _PALABRAS:
        if palabra in t:
            out.append(("kw", clave))
    return out


def construir_historial(clasificados: Iterable[tuple]) -> Dict[tuple, Counter]:
    """clasificados: (concepto, categoria_id)."""
    hist: Dict[tuple, Counter] = defaultdict(Counter)
    for concepto, cat in clasificados:
        for p in pistas(concepto):
            hist[p][cat] += 1
    return hist


def sugerir(concepto: Optional[str], hist: Dict[tuple, Counter]) -> Optional[dict]:
    mejores = []
    for p in pistas(concepto):
        h = hist.get(p)
        if not h:
            continue
        total = sum(h.values())
        cat, n = h.most_common(1)[0]
        mejores.append((_PRIORIDAD[p[0]], -total, p, cat, n, total))
    if not mejores:
        return None
    mejores.sort(key=lambda x: (x[0], x[1]))
    _, _, p, cat, n, total = mejores[0]
    acuerdo = n / total
    if total >= 2 and acuerdo >= 0.8:
        confianza = "alta"
    elif acuerdo >= 0.5:
        confianza = "media"
    else:
        return None
    return {"pista": p, "categoria_id": cat, "n": n, "total": total, "acuerdo": round(acuerdo, 2), "confianza": confianza}
