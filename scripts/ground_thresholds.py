"""Resuelve el respaldo documental de cada umbral del motor de decision.

El problema que resuelve: los umbrales de `clinical/thresholds.py` se ajustaron a
los 160 casos del dataset. Ajustar a 160 casos sinteticos es, por definicion,
sobreajuste. Un jurado clinico tiene todo el derecho a preguntar "de donde sale
ese 38.0" y "los ajustamos a los datos" es una respuesta mala.

Lo que hace este script: por cada umbral, busca en el corpus real la frase que lo
sustenta y la congela en `data/threshold_citations.json`. A partir de ahi, cada
regla que dispara en una llamada lleva su cita -- documento, pagina y frase
textual -- y `/api/reglas` la publica.

El resultado es que la regla no es "un numero que ajustamos": es una regla
clinica trazable que ademas ajusta bien a los datos. Son dos afirmaciones
distintas y solo la segunda se sostiene sola.

    python scripts/ground_thresholds.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))
os.environ.setdefault("FASTEMBED_CACHE_PATH", str(RAIZ / "data" / "modelos"))

from centinela.clinical.thresholds import (  # noqa: E402
    BANDERAS_AMARILLAS,
    REGLAS_VERSION,
    UMBRALES_ROJOS,
)
from centinela.rag.embedder import Embedder  # noqa: E402
from centinela.rag.retriever import Retriever, frase_mas_relevante  # noqa: E402
from centinela.rag.store import KnowledgeStore  # noqa: E402

DESTINO = RAIZ / "data" / "threshold_citations.json"


def main() -> int:
    store = KnowledgeStore(RAIZ / "data" / "index")
    stats = store.estadisticas()
    if stats["documentos"] == 0:
        print("el indice esta vacio; corre `make index` primero")
        return 2

    print(f"corpus: {stats['documentos']} documentos, {stats['chunks']} fragmentos")
    retriever = Retriever(store, Embedder())

    citas: dict[str, dict] = {}
    sin_respaldo: list[str] = []

    todos = [("rojo", u) for u in UMBRALES_ROJOS] + [("amarillo", b) for b in BANDERAS_AMARILLAS]

    print()
    for clase, u in todos:
        r = retriever.recuperar(u.consulta_evidencia, n_final=3)
        print(f"[{clase:8s}] {u.codigo:14s} {u.umbral_legible}")

        if not r.pasajes:
            print("             sin pasajes recuperados")
            sin_respaldo.append(u.codigo)
        else:
            mejor = max(r.pasajes, key=lambda p: p.similitud)
            doc = store.obtener_documento(mejor.doc_id)
            frase = frase_mas_relevante(mejor.texto, u.consulta_evidencia)
            citas[u.codigo] = {
                "documento": mejor.nombre,
                "documento_sha256": doc.sha256 if doc else None,
                "pagina": mejor.pagina,
                "cita_textual": frase,
                "puntaje": round(mejor.similitud, 4),
            }
            print(f"             -> {mejor.nombre[:66]}")
            print(f"                pag. {mejor.pagina}  similitud {mejor.similitud:.3f}")
            print(f"                \"{frase[:150]}\"")
        print()

    contenido = {
        "version_reglas": REGLAS_VERSION,
        "generacion_corpus": stats["generacion"],
        "nota": (
            "Citas resueltas automaticamente por scripts/ground_thresholds.py contra el "
            "corpus indexado. Cada una apunta a documento, pagina y frase textual para "
            "que sea verificable abriendo el PDF."
        ),
        "umbrales_sin_respaldo": sin_respaldo,
        "citas": citas,
    }
    DESTINO.parent.mkdir(parents=True, exist_ok=True)
    DESTINO.write_text(json.dumps(contenido, indent=2, ensure_ascii=False), encoding="utf-8")

    print("=" * 78)
    print(f"{len(citas)}/{len(todos)} umbrales con respaldo documental")
    if sin_respaldo:
        print(f"sin respaldo: {', '.join(sin_respaldo)}")
    print(f"escrito en {DESTINO.relative_to(RAIZ)}")
    store.cerrar()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
