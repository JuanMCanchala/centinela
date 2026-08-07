"""Empaqueta y desempaqueta el indice del corpus.

El indice se versiona comprimido (`data/index.zip`) en vez de como directorio.
Dos razones:

1. **La compuerta G2 del reto.** Reconstruir el indice desde los 107 PDFs toma
   cerca de una hora en esta maquina, sobre todo por el OCR de las paginas sin
   capa de texto. El jurado tiene 15 minutos para levantar la solucion, asi que
   el indice llega ya construido y `make up` solo lo extrae.

2. **Limites de GitHub.** El `chroma.sqlite3` descomprimido ronda los 70 MB, que
   dispara la advertencia de archivo grande. Comprimido baja bastante y ademas
   evita versionar de nuevo 90 MB cada vez que se reindexa.

La extraccion es idempotente y no destructiva: si `data/index/` ya existe con
contenido, no se toca. Eso importa porque un documento subido desde la consola
vive en ese directorio y no debe perderse al reiniciar.

    python scripts/empacar_index.py empacar
    python scripts/empacar_index.py extraer [--forzar]
"""

from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
DIR_INDEX = RAIZ / "data" / "index"
ZIP_INDEX = RAIZ / "data" / "index.zip"

# El WAL de SQLite no se empaqueta: se consolida antes de comprimir.
EXCLUIR = {".db-wal", ".db-shm", "-wal", "-shm"}


def _consolidar_wal() -> None:
    """Fuerza el checkpoint de los WAL para que el .db lleve todo el contenido."""

    import sqlite3

    for db in DIR_INDEX.glob("*.db"):
        try:
            conn = sqlite3.connect(db)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
        except Exception as e:  # noqa: BLE001
            print(f"  aviso: no pude consolidar {db.name}: {e}")

    chroma = DIR_INDEX / "chroma.sqlite3"
    if chroma.exists():
        try:
            conn = sqlite3.connect(chroma)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
        except Exception as e:  # noqa: BLE001
            print(f"  aviso: no pude consolidar chroma.sqlite3: {e}")


def empacar() -> int:
    if not DIR_INDEX.exists():
        print(f"no existe {DIR_INDEX}; corre `make index` primero")
        return 2

    print("consolidando WAL de SQLite...")
    _consolidar_wal()

    archivos = [
        p for p in DIR_INDEX.rglob("*")
        if p.is_file() and not any(str(p).endswith(s) for s in EXCLUIR)
    ]
    total = sum(p.stat().st_size for p in archivos)
    print(f"empacando {len(archivos)} archivos ({total/1024/1024:.1f} MB)...")

    ZIP_INDEX.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ZIP_INDEX, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(archivos):
            z.write(p, p.relative_to(DIR_INDEX))

    comprimido = ZIP_INDEX.stat().st_size
    print(f"{ZIP_INDEX.relative_to(RAIZ)}  {comprimido/1024/1024:.1f} MB "
          f"({100 * comprimido / total:.0f}% del original)")

    if comprimido > 95 * 1024 * 1024:
        print()
        print("ADVERTENCIA: el zip pasa de 95 MB y GitHub rechaza archivos de 100 MB.")
        print("Publicalo como asset de release y ajusta `make extraer` para bajarlo.")
    return 0


def extraer(forzar: bool = False) -> int:
    if not ZIP_INDEX.exists():
        print(f"no existe {ZIP_INDEX.relative_to(RAIZ)}")
        print("El indice se construye con `make index` (tarda cerca de una hora).")
        return 1

    ya_hay = DIR_INDEX.exists() and any(DIR_INDEX.iterdir())

    if ya_hay and not forzar:
        print(f"{DIR_INDEX.relative_to(RAIZ)} ya tiene contenido; no se toca.")
        print("Usa --forzar para reemplazarlo por el del zip.")
        codigo = 0
    else:
        if ya_hay:
            print(f"reemplazando {DIR_INDEX.relative_to(RAIZ)}")
            shutil.rmtree(DIR_INDEX)
        DIR_INDEX.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(ZIP_INDEX) as z:
            z.extractall(DIR_INDEX)
        n = len(list(DIR_INDEX.rglob("*")))
        tam = sum(p.stat().st_size for p in DIR_INDEX.rglob("*") if p.is_file())
        print(f"indice extraido: {n} entradas, {tam/1024/1024:.1f} MB")
        codigo = 0
    return codigo


def main() -> int:
    accion = sys.argv[1] if len(sys.argv) > 1 else "extraer"
    if accion == "empacar":
        codigo = empacar()
    elif accion == "extraer":
        codigo = extraer(forzar="--forzar" in sys.argv)
    else:
        print(__doc__)
        codigo = 2
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
