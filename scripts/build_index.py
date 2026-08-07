"""Construye el indice del corpus clinico y emite el informe de integridad.

Se corre una sola vez (`make index`) y su salida -- `data/index/` -- se
commitea al repositorio. Motivo: la compuerta G2 del reto da 15 minutos para
levantar la solucion, y no tiene sentido gastar varios de esos minutos
re-embebiendo 2.098 paginas que no han cambiado. El jurado levanta el sistema
con el indice ya hecho; la ingesta en caliente desde la consola sigue
funcionando igual para documentos nuevos.

El segundo producto de este script es `docs/informe-corpus.md`: un informe de
integridad que dice que se ingirio, que se descarto por duplicado, que paginas
necesitaron OCR y -- lo mas importante -- que documentos tienen un contenido que
no corresponde a la carpeta en la que estan.

    python scripts/build_index.py [--dataset RUTA] [--salida RUTA] [--limpiar]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))

# Cache estable de modelos: por defecto fastembed usa el TEMP del sistema, que
# se limpia y obliga a re-descargar 1.3 GB en el peor momento.
os.environ.setdefault("FASTEMBED_CACHE_PATH", str(RAIZ / "data" / "modelos"))

from centinela.rag.embedder import Embedder  # noqa: E402
from centinela.rag.ingest import (  # noqa: E402
    TEMA_DECLARADO_POR_CARPETA,
    TEMA_POR_PROCEDIMIENTO,
    chunkear,
    extraer_documento,
)
from centinela.rag.store import KnowledgeStore, sha256_bytes  # noqa: E402

LOTE_EMBED = 64


def descubrir_pdfs(base: Path) -> list[tuple[Path, str | None]]:
    """PDFs del corpus con el tema que declara su carpeta."""

    encontrados: list[tuple[Path, str | None]] = []
    for ruta in sorted(base.rglob("*.pdf")):
        carpeta = ruta.parent.name
        declarado = TEMA_DECLARADO_POR_CARPETA.get(carpeta)
        encontrados.append((ruta, declarado))
    return encontrados


@dataclass
class ResultadoDocumento:
    """Desenlace del procesamiento de un PDF.

    El bucle principal no toma decisiones: solo acumula estos resultados. Toda la
    ramificacion vive aqui dentro, con un unico punto de salida.
    """

    estado: str  # ingerido | ya_estaba | duplicado | sin_texto | vacio | error
    mensaje: str = ""
    paginas: int = 0
    chunks: int = 0
    paginas_ocr: int = 0
    tema: str | None = None
    nombre: str = ""
    duplicado_de: str | None = None
    huella: str | None = None
    incoherencia: dict | None = None
    error: str | None = None


def procesar_pdf(
    ruta: Path,
    declarado: str | None,
    store: KnowledgeStore,
    embedder: Embedder,
    con_ocr: bool,
) -> ResultadoDocumento:
    """Extrae, deduplica, chunkea, embebe e indexa un PDF."""

    sha = sha256_bytes(ruta.read_bytes())
    previo_archivo = store.existe_archivo(sha)

    if previo_archivo is not None:
        # Reanudacion: el archivo ya esta indexado. No se extrae de nuevo, que es
        # donde se va casi todo el tiempo por culpa del OCR.
        resultado = ResultadoDocumento(
            estado="ya_estaba",
            mensaje="ya indexado, se omite",
            paginas=previo_archivo.n_paginas,
            chunks=previo_archivo.n_chunks,
            paginas_ocr=previo_archivo.paginas_ocr,
            tema=previo_archivo.tema,
            nombre=previo_archivo.nombre,
        )
    else:
        try:
            doc = extraer_documento(ruta, tema_declarado=declarado, con_ocr=con_ocr)
        except Exception as e:  # noqa: BLE001
            resultado = ResultadoDocumento(
                estado="error",
                mensaje=f"ERROR extraccion: {type(e).__name__}: {e}",
                nombre=ruta.name,
                error=f"{type(e).__name__}: {e}",
            )
        else:
            if len(doc.texto_completo.strip()) < 200:
                resultado = ResultadoDocumento(
                    estado="sin_texto",
                    mensaje="SIN TEXTO UTIL, se omite",
                    paginas=len(doc.paginas),
                    paginas_ocr=doc.paginas_ocr,
                    nombre=doc.nombre,
                    error="sin capa de texto y OCR sin resultado",
                )
            else:
                # Dos niveles de dedup: huella exacta del texto y, si esa no
                # dispara, solape de terminos distintivos. El corpus del reto
                # necesita el segundo: sus dos pares de duplicados codifican el
                # mismo articulo con ligaduras distintas y la huella exacta no
                # los ve.
                previo = store.existe_contenido(doc.huella_texto)
                casi = None if previo else store.existe_casi_igual(doc.firma_bolsa)

                if previo is not None or casi is not None:
                    if previo is not None:
                        parecido = previo
                        detalle = "texto identico"
                    else:
                        parecido, similitud = casi
                        detalle = f"similitud {similitud:.3f}"
                    resultado = ResultadoDocumento(
                        estado="duplicado",
                        mensaje=f"DUPLICADO de {parecido.nombre[:40]} ({detalle}), se omite",
                        paginas=len(doc.paginas),
                        paginas_ocr=doc.paginas_ocr,
                        nombre=doc.nombre,
                        duplicado_de=parecido.nombre,
                        huella=doc.huella_texto[:16],
                    )
                else:
                    doc_id = doc.sha256[:32]
                    chunks = chunkear(doc, doc_id)
                    if not chunks:
                        resultado = ResultadoDocumento(
                            estado="vacio",
                            mensaje="0 chunks, se omite",
                            paginas=len(doc.paginas),
                            nombre=doc.nombre,
                        )
                    else:
                        vectores: list[list[float]] = []
                        for j in range(0, len(chunks), LOTE_EMBED):
                            lote = chunks[j:j + LOTE_EMBED]
                            vectores.extend(embedder.embed_pasajes([c.texto for c in lote]))

                        store.registrar_documento(
                            nombre=doc.nombre,
                            titulo=doc.titulo,
                            sha256=doc.sha256,
                            huella_texto=doc.huella_texto,
                            firma_bolsa=doc.firma_bolsa,
                            origen=f"corpus_oficial/{ruta.parent.name}",
                            categoria=ruta.parent.name,
                            tema=doc.tema_detectado,
                            n_paginas=len(doc.paginas),
                            paginas_ocr=doc.paginas_ocr,
                            chunks=chunks,
                            embeddings=vectores,
                        )
                        marca = " [OCR]" if doc.paginas_ocr else ""
                        alerta = " [TEMA INCOHERENTE]" if doc.incoherencia_tema else ""
                        resultado = ResultadoDocumento(
                            estado="ingerido",
                            mensaje=f"{len(doc.paginas):3d} pags  "
                                    f"{len(chunks):4d} chunks{marca}{alerta}",
                            paginas=len(doc.paginas),
                            chunks=len(chunks),
                            paginas_ocr=doc.paginas_ocr,
                            tema=doc.tema_detectado,
                            nombre=doc.nombre,
                            incoherencia={
                                "nombre": doc.nombre,
                                "carpeta": ruta.parent.name,
                                "tema_declarado_por_carpeta": doc.tema_declarado,
                                "tema_detectado_en_el_texto": doc.tema_detectado,
                                "puntajes": {k: v for k, v in doc.puntajes_tema.items() if v > 0},
                            } if doc.incoherencia_tema else None,
                        )

    return resultado


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dataset",
        default=str(RAIZ.parent / "ParticipantArtifacts" / "dataset" / "textos"),
        help="Carpeta con el corpus de PDFs",
    )
    ap.add_argument("--salida", default=str(RAIZ / "data" / "index"))
    ap.add_argument("--limpiar", action="store_true", help="Borra el indice antes de construir")
    ap.add_argument("--sin-ocr", action="store_true")
    args = ap.parse_args()

    base = Path(args.dataset)
    if not base.exists():
        print(f"ERROR: no existe {base}", file=sys.stderr)
        return 2

    salida = Path(args.salida)
    if args.limpiar and salida.exists():
        shutil.rmtree(salida)

    store = KnowledgeStore(salida)
    embedder = Embedder()
    print(f"modelo de embeddings: {embedder.nombre_modelo}")
    t0 = time.perf_counter()
    embedder.calentar()
    print(f"modelo listo en {time.perf_counter() - t0:.1f}s\n")

    pdfs = descubrir_pdfs(base)
    print(f"{len(pdfs)} PDFs encontrados en {base}\n")

    ingeridos = 0
    duplicados: list[dict] = []
    incoherencias: list[dict] = []
    sin_texto: list[dict] = []
    con_ocr: list[dict] = []
    total_chunks = 0
    total_paginas = 0
    por_tema: Counter = Counter()
    por_carpeta_tema: dict[str, Counter] = defaultdict(Counter)
    t_inicio = time.perf_counter()

    ya_estaban = 0

    # El bucle solo acumula: toda la ramificacion vive en procesar_pdf(), que
    # tiene un unico punto de salida.
    for i, (ruta, declarado) in enumerate(pdfs, start=1):
        etiqueta = f"[{i:3d}/{len(pdfs)}] {ruta.parent.name}/{ruta.name[:58]}"
        r = procesar_pdf(ruta, declarado, store, embedder, con_ocr=not args.sin_ocr)
        print(f"{etiqueta}  {r.mensaje}")

        total_paginas += r.paginas
        total_chunks += r.chunks

        if r.paginas_ocr:
            con_ocr.append({
                "nombre": r.nombre, "carpeta": ruta.parent.name,
                "paginas_ocr": r.paginas_ocr, "paginas": r.paginas,
            })

        if r.estado in ("ingerido", "ya_estaba"):
            ingeridos += 1
            clave_tema = r.tema or "sin_clasificar"
            por_tema[clave_tema] += 1
            por_carpeta_tema[ruta.parent.name][clave_tema] += 1
            if r.estado == "ya_estaba":
                ya_estaban += 1
            if r.incoherencia:
                incoherencias.append(r.incoherencia)
        elif r.estado == "duplicado":
            duplicados.append({
                "nombre": r.nombre, "carpeta": ruta.parent.name,
                "duplicado_de": r.duplicado_de, "huella": r.huella,
            })
        elif r.estado in ("sin_texto", "error"):
            sin_texto.append({
                "nombre": r.nombre, "carpeta": ruta.parent.name, "error": r.error,
            })

    duracion = time.perf_counter() - t_inicio

    # ------------------------------------------------------------------
    # Cobertura por procedimiento: el hueco clinico del corpus
    # ------------------------------------------------------------------
    cobertura = {}
    for procedimiento, tema in TEMA_POR_PROCEDIMIENTO.items():
        if procedimiento.endswith(("ia", "ía", "rodilla")):
            cobertura[procedimiento] = {
                "tema_requerido": tema,
                "documentos_disponibles": por_tema.get(tema, 0),
                "cubierto": por_tema.get(tema, 0) > 0,
            }

    resumen = {
        "modelo_embeddings": embedder.nombre_modelo,
        "dim": embedder.dim,
        "pdfs_encontrados": len(pdfs),
        "documentos_ingeridos": ingeridos,
        "duplicados_omitidos": duplicados,
        "sin_texto": sin_texto,
        "documentos_con_ocr": con_ocr,
        "incoherencias_de_tema": incoherencias,
        "total_paginas": total_paginas,
        "total_chunks": total_chunks,
        "por_tema": dict(por_tema),
        "por_carpeta_y_tema": {k: dict(v) for k, v in por_carpeta_tema.items()},
        "cobertura_por_procedimiento": cobertura,
        "generacion": store.generacion,
        "segundos": round(duracion, 1),
    }

    destino_json = RAIZ / "docs" / "metrics" / "corpus_index.json"
    destino_json.parent.mkdir(parents=True, exist_ok=True)
    destino_json.write_text(json.dumps(resumen, indent=2, ensure_ascii=False), encoding="utf-8")

    escribir_informe(resumen, RAIZ / "docs" / "informe-corpus.md")

    print()
    print("=" * 78)
    print(f"ingeridos          : {ingeridos}/{len(pdfs)}")
    print(f"paginas            : {total_paginas}")
    print(f"chunks             : {total_chunks}")
    print(f"duplicados omitidos: {len(duplicados)}")
    print(f"docs con OCR       : {len(con_ocr)}")
    print(f"sin texto util     : {len(sin_texto)}")
    print(f"tema incoherente   : {len(incoherencias)}   <-- revisar informe-corpus.md")
    print(f"tiempo             : {duracion:.1f}s")
    print(f"generacion         : {store.generacion}")
    print("=" * 78)
    for proc, c in cobertura.items():
        estado = "OK" if c["cubierto"] else "SIN COBERTURA"
        print(f"  {proc:30s} -> {c['tema_requerido']:24s} {c['documentos_disponibles']:3d} docs  {estado}")
    return 0


def escribir_informe(r: dict, destino: Path) -> None:
    L: list[str] = []
    L.append("# Informe de integridad del corpus clinico")
    L.append("")
    L.append("Generado automaticamente por `scripts/build_index.py`. No se edita a mano.")
    L.append("")
    L.append("## Resumen de ingesta")
    L.append("")
    L.append("| Metrica | Valor |")
    L.append("|---|---:|")
    L.append(f"| PDFs encontrados | {r['pdfs_encontrados']} |")
    L.append(f"| Documentos ingeridos | {r['documentos_ingeridos']} |")
    L.append(f"| Paginas procesadas | {r['total_paginas']} |")
    L.append(f"| Chunks indexados | {r['total_chunks']} |")
    L.append(f"| Duplicados omitidos | {len(r['duplicados_omitidos'])} |")
    L.append(f"| Documentos que requirieron OCR | {len(r['documentos_con_ocr'])} |")
    L.append(f"| Documentos sin texto util | {len(r['sin_texto'])} |")
    L.append(f"| Modelo de embeddings | `{r['modelo_embeddings']}` ({r['dim']}d) |")
    L.append(f"| Tiempo de construccion | {r['segundos']} s |")
    L.append("")

    L.append("## Defectos detectados en el material entregado")
    L.append("")

    if r["duplicados_omitidos"]:
        L.append("### Duplicados logicos")
        L.append("")
        L.append("Mismo contenido con nombre de archivo y bytes distintos, asi que ningun hash de")
        L.append("archivo los detecta. Se descartan comparando una huella del texto normalizado.")
        L.append("")
        L.append("| Documento omitido | Duplicado de | Carpeta |")
        L.append("|---|---|---|")
        for d in r["duplicados_omitidos"]:
            L.append(f"| `{d['nombre']}` | `{d['duplicado_de']}` | {d['carpeta']} |")
        L.append("")

    if r["documentos_con_ocr"]:
        L.append("### Documentos sin capa de texto (resueltos con OCR)")
        L.append("")
        L.append("| Documento | Carpeta | Paginas por OCR |")
        L.append("|---|---|---:|")
        for d in r["documentos_con_ocr"]:
            L.append(f"| `{d['nombre']}` | {d['carpeta']} | {d['paginas_ocr']}/{d['paginas']} |")
        L.append("")

    if r["incoherencias_de_tema"]:
        L.append("### Documentos cuyo contenido no corresponde a su carpeta")
        L.append("")
        L.append("**Este es el defecto con consecuencias clinicas.** Un RAG que enrute por nombre")
        L.append("de carpeta serviria estos documentos como si trataran del procedimiento de la")
        L.append("carpeta. Centinela clasifica el tema por el texto del documento, no por su")
        L.append("ubicacion, y la compuerta de fundamentacion se niega a responder cuando el")
        L.append("corpus no cubre el procedimiento del paciente.")
        L.append("")
        L.append("| Documento | Carpeta dice | El texto dice |")
        L.append("|---|---|---|")
        for d in r["incoherencias_de_tema"]:
            L.append(
                f"| `{d['nombre'][:60]}` | {d['tema_declarado_por_carpeta']} "
                f"| **{d['tema_detectado_en_el_texto']}** |"
            )
        L.append("")

    L.append("## Distribucion por tema detectado")
    L.append("")
    L.append("| Tema | Documentos |")
    L.append("|---|---:|")
    for tema, n in sorted(r["por_tema"].items(), key=lambda kv: -kv[1]):
        L.append(f"| {tema} | {n} |")
    L.append("")

    L.append("## Cobertura por procedimiento del paciente")
    L.append("")
    L.append("Los 40 pacientes del dataset se reparten en cinco procedimientos. Esta tabla dice")
    L.append("si el corpus entregado contiene material del procedimiento de cada uno.")
    L.append("")
    L.append("| Procedimiento | Tema requerido | Documentos | Estado |")
    L.append("|---|---|---:|---|")
    for proc, c in r["cobertura_por_procedimiento"].items():
        estado = "cubierto" if c["cubierto"] else "**SIN COBERTURA**"
        L.append(f"| {proc} | {c['tema_requerido']} | {c['documentos_disponibles']} | {estado} |")
    L.append("")

    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
