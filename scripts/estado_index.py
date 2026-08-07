"""Estado del indice. Util durante la construccion y para /health."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

RUTA = Path(sys.argv[1] if len(sys.argv) > 1 else "data/index/centinela.db")

if not RUTA.exists():
    print(f"no existe {RUTA}")
    raise SystemExit(0)

c = sqlite3.connect(str(RUTA))
docs = c.execute("SELECT COUNT(*) FROM documentos").fetchone()[0]
chunks = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
gen = c.execute("SELECT valor FROM meta WHERE clave = 'generacion'").fetchone()
paginas = c.execute("SELECT COALESCE(SUM(n_paginas), 0) FROM documentos").fetchone()[0]
ocr = c.execute("SELECT COUNT(*) FROM documentos WHERE paginas_ocr > 0").fetchone()[0]

print(f"documentos : {docs}")
print(f"paginas    : {paginas}")
print(f"chunks     : {chunks}")
print(f"con OCR    : {ocr}")
print(f"generacion : {gen[0] if gen else 0}")
print("\npor tema detectado:")
for tema, n in c.execute(
    "SELECT COALESCE(tema, 'sin_clasificar'), COUNT(*) FROM documentos "
    "GROUP BY tema ORDER BY 2 DESC"
):
    print(f"  {tema:26s} {n:3d}")
print("\npor carpeta de origen:")
for cat, n in c.execute(
    "SELECT COALESCE(categoria, '?'), COUNT(*) FROM documentos GROUP BY categoria ORDER BY 2 DESC"
):
    print(f"  {cat:26s} {n:3d}")
c.close()
