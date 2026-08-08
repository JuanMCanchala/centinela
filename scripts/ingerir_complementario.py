"""Tapa el unico hueco clinico del corpus entregado, sin disimularlo.

**El hueco.** `dataset/textos/breast_cancer/` trae 19 PDFs y ninguno es de cancer de mama:
todos son de cuello uterino. Mientras tanto el dataset tiene 8 pacientes con procedimiento
Mastectomia, el 20 % de la poblacion. Se comprobo contra el repo base
(`TechSphere2026/ParticipantArtifacts`) el 2026-08-08: el unico commit posterior a nuestra
copia toca `docs/stack-tecnico.md` y el dataset sigue igual. El hueco es del material.

Y es el hueco entero de la medicion: de las 60 preguntas de `eval/rag_cobertura.py`, las 12
que no se responden son **exactamente** las de mastectomia. Los otros cuatro procedimientos
van 12/12.

**Que hace este script.** Ingiere material publico real de autoridades nombradas sobre el
postoperatorio de cirugia de mama, con su procedencia registrada y marcado como
`complementario`. La marca no es decorativa: viaja hasta la cita, asi que cuando el agente
se apoya en este material lo dice, y `eval/rag_cobertura.py` puede informar las dos cifras
por separado -- lo que cubre el corpus oficial y lo que cubre con el complemento.

**Por que las dos cifras y no una.** Sumar documentos que responden nuestras propias
preguntas de evaluacion y presentar el resultado como una sola cifra seria inflar la
medicion. La cifra del corpus oficial sigue siendo la que compara con el material que el
reto entrego.

**Lo que NO se toca.** `make auditar` sigue reportando los 18 documentos mal archivados del
corpus entregado. El hallazgo no se borra por haberlo resuelto.

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
    "fredhutch_guia_operacion_de_mama.pdf": {
        "titulo": "Guia para la operacion de mama",
        "autoridad": "Fred Hutchinson Cancer Center / UW Medicine (Seattle)",
        "url": "https://patient-education.fredhutch.org/documents/"
               "Guide%20to%20Your%20Breast%20Surgery_Spanish.pdf",
        "descargado_en": "2026-08-08",
        "que_cubre": "preoperatorio, drenajes, cuidado de la herida, ducha, actividad, "
                     "cuando llamar al equipo clinico",
    },
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
