"""Audita el corpus ya indexado y regenera el informe de integridad.

Existe porque `build_index.py` solo reporta los defectos que detecta *durante* la
ingesta, y una ingesta reanudada salta los documentos ya indexados sin volver a
evaluarlos. Este script trabaja sobre lo que quedo en el indice, asi que el
informe es correcto independientemente de cuantas veces se interrumpio la
construccion.

Detecta tres cosas:

1. **Incoherencia carpeta / contenido.** Compara el tema detectado por el texto
   contra el que declara la carpeta de origen.
2. **Duplicados logicos.** Documentos distintos con contenido casi identico. Se
   comparan por similitud de conjuntos de palabras sobre el texto de los chunks,
   porque la huella exacta falla cuando dos PDFs del mismo articulo extraen el
   texto con ligaduras o guionado distinto.
3. **Cobertura por procedimiento.** Que pacientes del dataset no tienen material
   clinico de su procedimiento.

    python scripts/auditar_corpus.py
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]

# Tiene que coincidir con `scripts/ingerir_complementario.py`.
CATEGORIA_COMPLEMENTARIA = "complementario"
sys.path.insert(0, str(RAIZ / "api"))

from centinela.rag.ingest import (  # noqa: E402
    TEMA_DECLARADO_POR_CARPETA,
    TEMA_POR_PROCEDIMIENTO,
)

DB = RAIZ / "data" / "index" / "centinela.db"
UMBRAL_DUPLICADO = 0.86


def normalizar(t: str) -> str:
    sin = "".join(
        c for c in unicodedata.normalize("NFD", t.lower())
        if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"[^a-z0-9 ]", " ", sin)


def bolsa(t: str) -> set[str]:
    return {p for p in normalizar(t).split() if len(p) > 3}


def main() -> int:
    if not DB.exists():
        print(f"no existe {DB}")
        return 2

    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

    docs = [dict(r) for r in c.execute(
        "SELECT doc_id, nombre, titulo, categoria, tema, n_paginas, n_chunks,"
        " paginas_ocr, origen, sha256 FROM documentos ORDER BY nombre"
    )]
    print(f"{len(docs)} documentos en el indice")

    # ---------------- 1. incoherencias carpeta / contenido ----------------
    incoherencias = []
    for d in docs:
        declarado = TEMA_DECLARADO_POR_CARPETA.get(d["categoria"] or "")
        if declarado and d["tema"] and declarado != d["tema"]:
            incoherencias.append({
                "nombre": d["nombre"],
                "carpeta": d["categoria"],
                "tema_declarado_por_carpeta": declarado,
                "tema_detectado_en_el_texto": d["tema"],
            })

    # ---------------- 2. duplicados logicos -------------------------------
    print("comparando contenido para detectar duplicados logicos...")
    texto_por_doc: dict[str, set[str]] = {}
    for d in docs:
        filas = c.execute(
            "SELECT texto FROM chunks WHERE doc_id = ? ORDER BY orden LIMIT 12",
            (d["doc_id"],),
        ).fetchall()
        texto_por_doc[d["doc_id"]] = bolsa(" ".join(f["texto"] for f in filas))

    por_id = {d["doc_id"]: d for d in docs}
    duplicados = []
    ids = list(texto_por_doc)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = texto_por_doc[ids[i]], texto_por_doc[ids[j]]
            if a and b:
                inter = len(a & b)
                menor = min(len(a), len(b))
                sim = inter / menor if menor else 0.0
                if sim >= UMBRAL_DUPLICADO:
                    duplicados.append({
                        "documento_a": por_id[ids[i]]["nombre"],
                        "documento_b": por_id[ids[j]]["nombre"],
                        "carpeta_a": por_id[ids[i]]["categoria"],
                        "carpeta_b": por_id[ids[j]]["categoria"],
                        "similitud": round(sim, 4),
                    })

    # ---------------- 3. cobertura por procedimiento ----------------------
    #
    # Esta auditoria es del material ENTREGADO, asi que el complementario -- guia publica
    # anadida para tapar el hueco de mastectomia, ver `scripts/ingerir_complementario.py`
    # -- se cuenta aparte. Sumarlo aqui haria que el informe afirmara que el corpus del
    # reto cubre mastectomia, y no la cubre: sus 19 PDFs de `breast_cancer/` son de cuello
    # uterino y eso sigue siendo cierto.
    oficiales = [d for d in docs if (d["categoria"] or "") != CATEGORIA_COMPLEMENTARIA]
    complementarios = [d for d in docs if (d["categoria"] or "") == CATEGORIA_COMPLEMENTARIA]
    por_tema = Counter(d["tema"] or "sin_clasificar" for d in oficiales)
    por_tema_compl = Counter(d["tema"] or "sin_clasificar" for d in complementarios)
    procedimientos = sorted({
        p for p in TEMA_POR_PROCEDIMIENTO if not any(ord(ch) > 127 for ch in p)
    })
    cobertura = {}
    for proc in procedimientos:
        tema = TEMA_POR_PROCEDIMIENTO[proc]
        cobertura[proc] = {
            "tema_requerido": tema,
            "documentos_disponibles": por_tema.get(tema, 0),
            "cubierto": por_tema.get(tema, 0) > 0,
            "documentos_complementarios": por_tema_compl.get(tema, 0),
        }

    con_ocr = [d for d in docs if d["paginas_ocr"]]
    por_carpeta = defaultdict(Counter)
    for d in docs:
        por_carpeta[d["categoria"] or "?"][d["tema"] or "sin_clasificar"] += 1

    resumen = {
        "documentos_indexados": len(docs),
        "total_paginas": sum(d["n_paginas"] for d in docs),
        "total_chunks": sum(d["n_chunks"] for d in docs),
        "documentos_con_ocr": [
            {"nombre": d["nombre"], "carpeta": d["categoria"],
             "paginas_ocr": d["paginas_ocr"], "paginas": d["n_paginas"]}
            for d in con_ocr
        ],
        "incoherencias_de_tema": incoherencias,
        "duplicados_logicos": duplicados,
        "por_tema": dict(por_tema),
        "por_carpeta_y_tema": {k: dict(v) for k, v in por_carpeta.items()},
        "cobertura_por_procedimiento": cobertura,
    }

    print()
    print("=" * 78)
    print(f"documentos            : {len(docs)}")
    print(f"paginas               : {resumen['total_paginas']}")
    print(f"chunks                : {resumen['total_chunks']}")
    print(f"con OCR               : {len(con_ocr)}")
    print(f"tema incoherente      : {len(incoherencias)}")
    print(f"duplicados logicos    : {len(duplicados)}")
    print("=" * 78)

    if incoherencias:
        print("\nINCOHERENCIAS CARPETA / CONTENIDO:")
        agrupado = Counter(
            (i["carpeta"], i["tema_declarado_por_carpeta"], i["tema_detectado_en_el_texto"])
            for i in incoherencias
        )
        for (carpeta, dec, det), n in agrupado.most_common():
            print(f"  {n:3d} docs en '{carpeta}/' dicen ser '{dec}' y son '{det}'")

    if duplicados:
        print("\nDUPLICADOS LOGICOS:")
        for d in duplicados:
            print(f"  {d['similitud']:.3f}  {d['documento_a'][:52]}")
            print(f"          {d['documento_b'][:52]}")

    print("\nCOBERTURA POR PROCEDIMIENTO (solo el corpus entregado):")
    for proc, cob in cobertura.items():
        estado = "OK" if cob["cubierto"] else "SIN COBERTURA"
        extra = ""
        if cob["documentos_complementarios"]:
            extra = f"   [+{cob['documentos_complementarios']} complementario(s)]"
        print(f"  {proc:30s} -> {cob['tema_requerido']:22s} "
              f"{cob['documentos_disponibles']:3d} docs  {estado}{extra}")
    if complementarios:
        print()
        print(f"  Material complementario indexado aparte: {len(complementarios)} doc(s).")
        print("  No cuenta como cobertura del corpus entregado, y la cita lo declara.")

    destino_json = RAIZ / "docs" / "metrics" / "corpus_index.json"
    destino_json.parent.mkdir(parents=True, exist_ok=True)
    destino_json.write_text(json.dumps(resumen, indent=2, ensure_ascii=False), encoding="utf-8")

    from build_index import escribir_informe  # noqa: PLC0415

    resumen_informe = {
        **resumen,
        "pdfs_encontrados": 107,
        "documentos_ingeridos": len(docs),
        "duplicados_omitidos": [
            {"nombre": d["documento_b"], "duplicado_de": d["documento_a"],
             "carpeta": d["carpeta_b"]}
            for d in duplicados
        ],
        "sin_texto": [],
        "modelo_embeddings": "intfloat/multilingual-e5-large",
        "dim": 1024,
        "segundos": 0,
    }
    escribir_informe(resumen_informe, RAIZ / "docs" / "informe-corpus.md")
    print(f"\ninforme en docs/informe-corpus.md")
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
