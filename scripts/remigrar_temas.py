"""Recalcula tema y firma de contenido sobre el indice ya construido.

Se necesita cuando cambia el lexico de clasificacion de `rag/ingest.py` o cuando
se agrega un mecanismo de dedup nuevo. Reconstruir el indice desde los PDFs tarda
siete minutos por el OCR; esto tarda segundos, porque el texto ya esta guardado en
la tabla `chunks`.

Actualiza las dos copias del tema, y eso es lo importante: SQLite y los metadatos
de Chroma. Si solo se actualizara SQLite, el filtro por tema de la recuperacion
densa seguiria usando el valor viejo -- silenciosamente, porque el filtro se
aplica del lado de Chroma.

    python scripts/remigrar_temas.py [--seco]
"""

from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))

from centinela.rag.ingest import clasificar_tema, normalizar  # noqa: E402
from centinela.rag.store import KnowledgeStore  # noqa: E402

SECO = "--seco" in sys.argv


def firma_desde_texto(texto: str) -> str:
    """Misma logica que DocumentoExtraido.firma_bolsa, sobre texto ya extraido."""

    palabras = [p for p in normalizar(texto).split() if len(p) > 4]
    frecuencias: dict[str, int] = {}
    for p in palabras[:40000]:
        frecuencias[p] = frecuencias.get(p, 0) + 1
    distintivos = sorted(frecuencias, key=lambda w: (-frecuencias[w], w))[:300]
    return " ".join(sorted(distintivos))


def main() -> int:
    store = KnowledgeStore(RAIZ / "data" / "index")
    conn = sqlite3.connect(store.ruta_sqlite)
    conn.row_factory = sqlite3.Row

    docs = [dict(r) for r in conn.execute(
        "SELECT doc_id, nombre, categoria, tema FROM documentos ORDER BY nombre"
    )]
    print(f"{len(docs)} documentos" + ("  (simulacion, no se escribe)" if SECO else ""))

    cambios_tema: list[tuple[str, str | None, str | None]] = []
    firmas = 0

    for d in docs:
        filas = conn.execute(
            "SELECT texto FROM chunks WHERE doc_id = ? ORDER BY orden", (d["doc_id"],)
        ).fetchall()
        texto = " ".join(f["texto"] for f in filas)

        nuevo_tema, _puntajes = clasificar_tema(texto)
        firma = firma_desde_texto(texto)

        if nuevo_tema != d["tema"]:
            cambios_tema.append((d["nombre"], d["tema"], nuevo_tema))
            print(f"  TEMA  {d['nombre'][:56]}")
            print(f"        {d['tema']} -> {nuevo_tema}")

        if not SECO:
            conn.execute(
                "UPDATE documentos SET tema = ?, firma_bolsa = ? WHERE doc_id = ?",
                (nuevo_tema, firma, d["doc_id"]),
            )
            firmas += 1

            if nuevo_tema != d["tema"]:
                # La copia del tema que vive en los metadatos de Chroma es la que
                # usa el filtro de la busqueda densa. Sin esto, el filtro seguiria
                # con el valor viejo y no habria forma de notarlo.
                ids = [r["chunk_id"] for r in conn.execute(
                    "SELECT chunk_id FROM chunks WHERE doc_id = ?", (d["doc_id"],)
                )]
                if ids:
                    actual = store.coleccion.get(ids=ids, include=["metadatas"])
                    metas = actual.get("metadatas") or []
                    nuevas = []
                    for m in metas:
                        m = dict(m or {})
                        m["tema"] = nuevo_tema or ""
                        nuevas.append(m)
                    store.coleccion.update(ids=actual.get("ids") or ids, metadatas=nuevas)

    if not SECO:
        conn.commit()

    print()
    print("=" * 74)
    print(f"  temas corregidos : {len(cambios_tema)}")
    print(f"  firmas escritas  : {firmas}")

    distrib = Counter(
        r["tema"] or "sin_clasificar"
        for r in conn.execute("SELECT tema FROM documentos")
    )
    print("\n  distribucion por tema:")
    for tema, n in distrib.most_common():
        print(f"    {tema:26s} {n:3d}")

    conn.close()
    store.cerrar()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
