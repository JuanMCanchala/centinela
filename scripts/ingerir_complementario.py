"""Demuestra la ingesta de material marcado, sin fingir que tapa el hueco del corpus.

**El hueco, y lo que resulto ser.** `dataset/textos/breast_cancer/` trae 19 PDFs y ninguno
es de cancer de mama: todos son de cuello uterino. Mientras tanto el dataset tiene 8
pacientes con procedimiento Mastectomia, el 20 % de la poblacion.

**La organizacion lo confirmo por correo el 2026-08-08: el desajuste era INTENCIONAL**,
puesto ahi "para evaluar el criterio y la capacidad analitica de cada concursante". Y sobre
complementar con guia publica marcada dijeron que esta bien planteado, "sin embargo, ten en
cuenta que el enfoque correcto debe ser corregir y ajustar el modelo en si".

Eso cambia lo que este script significa. La respuesta correcta al hueco **no** es traer
documentos: es que el sistema detecte que no tiene cobertura y se niegue a responder. Eso
ya lo hace, y por dos caminos que no dependen de la carpeta:

  - `rag/ingest.py` clasifica el tema por el TEXTO del documento, no por el directorio, asi
    que los 19 PDFs mal archivados quedan clasificados como cuello uterino y nunca se
    ofrecen a un paciente de mama.
  - `rag/retriever.py` se abstiene cuando el corpus no cubre el procedimiento, con la razon
    registrada. Medido: 11 de las 12 preguntas de mastectomia terminan en abstencion
    honesta, 0 citas cruzadas.

**Que queda entonces de este script.** Un solo folleto (23 fragmentos), como demostracion
de que el sistema puede ingerir material externo con su procedencia declarada y que la
marca `complementario` viaja hasta la cita. El de Fred Hutchinson se retiro a
`data/complementario/retirados/`: aportaba 139 fragmentos y 2.3 MB para una cobertura que,
segun la propia organizacion, no es lo que se evalua.

**Por que las dos cifras y no una.** Sumar documentos que responden nuestras propias
preguntas de evaluacion y presentar el resultado como una sola cifra seria inflar la
medicion. La cifra del corpus oficial -- 48/60 -- sigue siendo la que compara con el
material que el reto entrego.

**Lo que NO se toca.** `make auditar` sigue reportando los 18 documentos mal archivados del
corpus entregado. Ahora se sabe que estaban puestos a proposito, y el hallazgo es
precisamente lo que habia que encontrar.

    python scripts/ingerir_complementario.py [--listar]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))

from centinela.config import config  # noqa: E402
from centinela.rag.embedder import Embedder  # noqa: E402
from centinela.rag.ingest import chunkear, extraer_documento  # noqa: E402
from centinela.rag.store import KnowledgeStore  # noqa: E402

DIR = RAIZ / "data" / "complementario"
MANIFIESTO = DIR / "procedencia.json"

# La categoria con la que se marca todo lo que entra por aqui. `rag/retriever.py` la
# propaga al pasaje y `rag/answerer.py` a la cita.
CATEGORIA = "complementario"

# De donde salio cada documento. Va al manifiesto y de ahi al informe: una fuente clinica
# sin procedencia no es una fuente, es un texto.
FUENTES = {
    "mskcc_ejercicios_tras_mastectomia.pdf": {
        "titulo": "Ejercicios para hacer despues de su mastectomia o reconstruccion de mama",
        "autoridad": "Memorial Sloan Kettering Cancer Center",
        "url": "https://www.mskcc.org/es/pdf/cancer-care/patient-education/"
               "exercises-after-mastectomy-or-reconstruction",
        "descargado_en": "2026-08-08",
        "que_cubre": "movilidad del brazo y del hombro, progresion de la actividad, "
                     "senales de alarma al ejercitarse",
    },
}

# Retirado del indice, no borrado. Sigue en `data/complementario/retirados/` con su
# procedencia por si hiciera falta reponerlo: `make complementario` no lo mira porque el
# script solo recorre los PDF de `data/complementario/`.
RETIRADOS = {
    "fredhutch_guia_operacion_de_mama.pdf": {
        "titulo": "Guia para la operacion de mama",
        "autoridad": "Fred Hutchinson Cancer Center / UW Medicine (Seattle)",
        "url": "https://patient-education.fredhutch.org/documents/"
               "Guide%20to%20Your%20Breast%20Surgery_Spanish.pdf",
        "retirado_en": "2026-08-08",
        "por_que": "139 fragmentos y 2.3 MB para cubrir un hueco que la organizacion "
                   "confirmo intencional y que se evalua por la abstencion, no por la "
                   "cobertura",
    },
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listar", action="store_true",
                    help="solo muestra que hay y con que tema se clasificaria")
    args = ap.parse_args()

    pdfs = sorted(DIR.glob("*.pdf"))
    if not pdfs:
        print(f"no hay PDF en {DIR}")
        return 1

    salida = 0
    config.asegurar_directorios()
    store = None if args.listar else KnowledgeStore(config.dir_index)
    embedder = None if args.listar else Embedder()
    registrados = []

    for ruta in pdfs:
        meta = FUENTES.get(ruta.name)

        if meta is None:
            # Un PDF sin procedencia declarada no entra. Es la regla que impide que
            # aparezca material en el corpus clinico sin que nadie sepa de donde vino.
            print(f"  OMITIDO  {ruta.name}: sin procedencia en FUENTES")
            salida = 1
        else:
            doc = extraer_documento(ruta, con_ocr=True)
            print(f"  {ruta.name}")
            print(f"    {len(doc.paginas)} pags · tema detectado: {doc.tema_detectado} · "
                  f"{meta['autoridad']}")

            if doc.tema_detectado != "cancer_mama":
                # Se comprueba el tema por el TEXTO, igual que con el corpus oficial. Un
                # folleto de reduccion mamaria no es material de mastectomia, y meterlo
                # seria repetir el mismo defecto que este proyecto documenta del corpus.
                print("    RECHAZADO: el texto no clasifica como cancer_mama")
                salida = 1
            elif not args.listar:
                chunks = chunkear(doc, doc.sha256[:32])
                vectores = embedder.embed_pasajes([c.texto for c in chunks])
                reg = store.registrar_documento(
                    nombre=doc.nombre,
                    titulo=meta["titulo"],
                    sha256=doc.sha256,
                    huella_texto=doc.huella_texto,
                    firma_bolsa=doc.firma_bolsa,
                    origen=f"complementario/{meta['autoridad']}",
                    categoria=CATEGORIA,
                    tema=doc.tema_detectado,
                    n_paginas=len(doc.paginas),
                    paginas_ocr=doc.paginas_ocr,
                    chunks=chunks,
                    embeddings=vectores,
                )
                print(f"    ingerido: {reg.n_chunks} fragmentos · "
                      f"generacion {reg.generacion}")
                registrados.append({
                    "doc_id": reg.doc_id, "nombre": reg.nombre, "sha256": reg.sha256,
                    "n_chunks": reg.n_chunks, "n_paginas": reg.n_paginas, **meta,
                })

    if registrados:
        MANIFIESTO.write_text(
            json.dumps({"categoria": CATEGORIA, "documentos": registrados},
                       indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\nprocedencia escrita en {MANIFIESTO.relative_to(RAIZ)}")

    return salida


if __name__ == "__main__":
    raise SystemExit(main())
