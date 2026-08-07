"""Ingesta del corpus clinico: PDF -> texto -> chunks con pagina.

Tres cosas que este ingestor hace y que un ingestor generico no:

1. **OCR selectivo.** El corpus del reto trae un PDF escaneado sin capa de texto
   (`Appendicitis/REVISION DE LA LITERATURA SOBRE LAAPENDICITIS AGUDA
   PEDIATRICA...pdf`, 0 caracteres extraibles). En vez de descartarlo o de
   OCR-ear los 107 documentos, se detecta pagina por pagina y solo se OCR-ea la
   que lo necesita. Se registra cuantas paginas pasaron por OCR, porque un texto
   de OCR es menos confiable y eso debe quedar visible en la cita.

2. **Dedup por contenido, no por bytes.** El corpus trae el mismo articulo dos
   veces con nombres distintos y bytes distintos. La huella se calcula sobre el
   texto normalizado.

3. **Clasificacion de tema por contenido y deteccion de incoherencias.** La
   carpeta `breast_cancer/` del corpus oficial contiene 19 documentos y ninguno
   es de cancer de mama: todos son de cancer de cuello uterino. Mientras tanto
   hay 8 pacientes con procedimiento "Mastectomia". Si el RAG enruta por nombre
   de carpeta, le sirve guias de cervix a una paciente mastectomizada, que es
   exactamente la alucinacion clinica peligrosa que la rubrica penaliza. Aqui el
   tema se deduce del texto y toda discrepancia carpeta-vs-contenido queda en el
   informe de integridad del corpus.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader

from .store import ChunkIngerido, sha256_bytes, sha256_texto

MIN_CARACTERES_PAGINA = 120
TAM_CHUNK = 1100
SOLAPE_CHUNK = 160
MIN_CHUNK = 240


# --------------------------------------------------------------------------
# Clasificacion de tema por contenido
# --------------------------------------------------------------------------

LEXICOS_TEMA: dict[str, tuple[str, ...]] = {
    "apendicitis": (
        "apendicitis", "apendicectomia", "appendicitis", "appendectomy",
        "appendicectomy", "apendice", "appendiceal",
    ),
    "colecistitis": (
        "colecistitis", "colecistectomia", "cholecystitis", "cholecystectomy",
        "vesicula biliar", "gallbladder", "colelitiasis", "gallstone", "biliar",
    ),
    "cancer_colorrectal": (
        "colorrectal", "colorectal", "colectomia", "colectomy", "cancer de colon",
        "colon cancer", "recto", "rectal", "colostomia", "colostomy",
        # "eras" NO va aqui. Enhanced Recovery After Surgery es un protocolo
        # transversal: el corpus lo usa tanto en cirugia colorrectal como en
        # artroplastia. Tenerlo en este lexico hacia que dos guias de reemplazo
        # articular se clasificaran como cancer colorrectal, y eso rompe el
        # filtro de tema de la recuperacion justo donde mas importa.
    ),
    "cancer_mama": (
        "cancer de mama", "breast cancer", "mastectomia", "mastectomy",
        "mamario", "mammary", "cuadrantectomia", "ganglio centinela axilar",
    ),
    "cancer_cuello_uterino": (
        "cuello uterino", "cervical cancer", "cervix", "cervicouterino",
        "histerectomia radical", "radical hysterectomy", "citologia cervico",
        "papanicolaou", "vph", "hpv",
    ),
    "artroplastia": (
        "artroplastia", "arthroplasty", "reemplazo total de rodilla",
        "reemplazo de cadera", "knee replacement", "hip replacement",
        "protesis", "prosthesis", "rodilla", "cadera",
    ),
}

# Como se llaman las carpetas del corpus oficial y que tema declaran.
TEMA_DECLARADO_POR_CARPETA = {
    "appendicitis": "apendicitis",
    "cholecystitis": "colecistitis",
    "colorectal cancer": "cancer_colorrectal",
    "breast_cancer": "cancer_mama",
    "total joint replacement": "artroplastia",
}

# Procedimiento del paciente -> tema del corpus que deberia cubrirlo.
TEMA_POR_PROCEDIMIENTO = {
    "Apendicectomia": "apendicitis",
    "Apendicectomía": "apendicitis",
    "Colecistectomia": "colecistitis",
    "Colecistectomía": "colecistitis",
    "Colectomia": "cancer_colorrectal",
    "Colectomía": "cancer_colorrectal",
    "Mastectomia": "cancer_mama",
    "Mastectomía": "cancer_mama",
    "Reemplazo de cadera/rodilla": "artroplastia",
}


def normalizar(texto: str) -> str:
    """Minusculas sin tildes, para comparar lexico de forma robusta."""

    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", texto.lower())
        if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"\s+", " ", sin_tildes)


def clasificar_tema(texto: str) -> tuple[str | None, dict[str, int]]:
    """Tema dominante del documento segun su propio texto."""

    muestra = normalizar(texto[:20000])
    puntajes = {
        tema: sum(muestra.count(term) for term in terminos)
        for tema, terminos in LEXICOS_TEMA.items()
    }
    positivos = {t: p for t, p in puntajes.items() if p > 0}

    if not positivos:
        tema = None
    else:
        tema = max(positivos, key=positivos.get)
    return tema, puntajes


# --------------------------------------------------------------------------
# Extraccion
# --------------------------------------------------------------------------

@dataclass
class PaginaExtraida:
    numero: int
    texto: str
    por_ocr: bool = False


@dataclass
class DocumentoExtraido:
    nombre: str
    ruta: Path
    sha256: str
    titulo: str | None
    paginas: list[PaginaExtraida] = field(default_factory=list)
    tema_detectado: str | None = None
    tema_declarado: str | None = None
    puntajes_tema: dict[str, int] = field(default_factory=dict)

    @property
    def texto_completo(self) -> str:
        return "\n".join(p.texto for p in self.paginas)

    @property
    def paginas_ocr(self) -> int:
        return sum(1 for p in self.paginas if p.por_ocr)

    @property
    def huella_texto(self) -> str:
        """Huella exacta del contenido, insensible a formato y a bytes."""

        base = normalizar(self.texto_completo)
        solo_letras = re.sub(r"[^a-z0-9 ]", "", base)
        return sha256_texto(solo_letras[:60000])

    @property
    def firma_bolsa(self) -> str:
        """Firma para duplicados *casi* identicos.

        La huella exacta no basta y esto se descubrio auditando el corpus del
        reto. Hay dos pares de documentos que son el mismo articulo con nombre de
        archivo distinto:

          - "Orthopaedic Surgery - 2019 - Li - Postoperative Pain Management in
            Total Knee Arthroplasty.pdf" y "Postoperative Pain Management in
            Total Knee Arthroplasty.pdf"  (similitud 1.000)
          - "Recommendations for follow-up of colorectal cancer survivors.pdf" y
            "ecommendations for follow-up of colorectal cancer survivors.pdf"
            (mismo DOI 10.1007/s12094-019-02059-1, similitud 0.998)

        Ninguno de los dos pares tiene el mismo sha256 de archivo NI la misma
        huella de texto exacta, porque los dos PDFs codifican ligaduras y
        guionado de forma distinta. Lo que si comparten es el vocabulario. Esta
        firma guarda los terminos distintivos ordenados para poder comparar por
        solape de conjuntos.
        """

        palabras = [p for p in normalizar(self.texto_completo).split() if len(p) > 4]
        # Los terminos mas frecuentes de un documento largo son su tema; son los
        # que mejor lo identifican y los mas estables frente a errores de
        # extraccion puntuales.
        frecuencias: dict[str, int] = {}
        for p in palabras[:40000]:
            frecuencias[p] = frecuencias.get(p, 0) + 1
        distintivos = sorted(frecuencias, key=lambda w: (-frecuencias[w], w))[:300]
        return " ".join(sorted(distintivos))

    @property
    def incoherencia_tema(self) -> bool:
        hay = (
            self.tema_declarado is not None
            and self.tema_detectado is not None
            and self.tema_declarado != self.tema_detectado
        )
        return hay


class _MotorOCR:
    """rapidocr + pypdfium2. Se carga solo si hay una pagina que lo necesite."""

    def __init__(self) -> None:
        self._ocr = None

    def disponible(self) -> bool:
        try:
            import pypdfium2  # noqa: F401
            from rapidocr_onnxruntime import RapidOCR  # noqa: F401
            ok = True
        except ImportError:
            ok = False
        return ok

    def texto_de_pagina(self, ruta_pdf: Path, indice_pagina: int) -> str:
        import numpy as np
        import pypdfium2 as pdfium
        from rapidocr_onnxruntime import RapidOCR

        if self._ocr is None:
            self._ocr = RapidOCR()

        pdf = pdfium.PdfDocument(str(ruta_pdf))
        pagina = pdf[indice_pagina]
        imagen = pagina.render(scale=2.2).to_pil()
        resultado, _ = self._ocr(np.array(imagen))
        pdf.close()

        if not resultado:
            texto = ""
        else:
            texto = " ".join(linea[1] for linea in resultado)
        return texto


_OCR = _MotorOCR()


def extraer_documento(ruta: Path, tema_declarado: str | None = None, con_ocr: bool = True) -> DocumentoExtraido:
    datos = ruta.read_bytes()
    lector = PdfReader(str(ruta))

    paginas: list[PaginaExtraida] = []
    for i, pagina in enumerate(lector.pages):
        try:
            crudo = pagina.extract_text() or ""
        except Exception:
            crudo = ""
        limpio = _limpiar(crudo)
        necesita_ocr = len(limpio) < MIN_CARACTERES_PAGINA and con_ocr and _OCR.disponible()

        if necesita_ocr:
            try:
                limpio_ocr = _limpiar(_OCR.texto_de_pagina(ruta, i))
            except Exception:
                limpio_ocr = ""
            if len(limpio_ocr) > len(limpio):
                paginas.append(PaginaExtraida(numero=i + 1, texto=limpio_ocr, por_ocr=True))
            else:
                paginas.append(PaginaExtraida(numero=i + 1, texto=limpio))
        else:
            paginas.append(PaginaExtraida(numero=i + 1, texto=limpio))

    doc = DocumentoExtraido(
        nombre=ruta.name,
        ruta=ruta,
        sha256=sha256_bytes(datos),
        titulo=_titulo(paginas, ruta),
        paginas=paginas,
        tema_declarado=tema_declarado,
    )
    doc.tema_detectado, doc.puntajes_tema = clasificar_tema(doc.texto_completo)
    return doc


def _limpiar(texto: str) -> str:
    sin_guiones = re.sub(r"-\n(?=[a-zaeiouñ])", "", texto)
    unificado = re.sub(r"[ \t]+", " ", sin_guiones)
    sin_saltos = re.sub(r"\n{2,}", "\n", unificado)
    return sin_saltos.strip()


def _titulo(paginas: list[PaginaExtraida], ruta: Path) -> str | None:
    if not paginas or not paginas[0].texto:
        titulo = ruta.stem
    else:
        candidatas = [
            l.strip() for l in paginas[0].texto.split("\n")
            if 18 <= len(l.strip()) <= 200
        ]
        titulo = candidatas[0] if candidatas else ruta.stem
    return titulo


# --------------------------------------------------------------------------
# Chunking con pagina
# --------------------------------------------------------------------------

def chunkear(doc: DocumentoExtraido, doc_id: str) -> list[ChunkIngerido]:
    """Ventanas solapadas respetando frontera de pagina.

    No se mezclan paginas dentro de un chunk: es lo que permite que la cita diga
    "pagina 7" y que el jurado abra el PDF en la pagina 7 y encuentre la frase.
    """

    chunks: list[ChunkIngerido] = []
    orden = 0
    vistos: set[str] = set()

    for pagina in doc.paginas:
        texto = pagina.texto.strip()
        if len(texto) >= MIN_CHUNK // 3:
            for trozo in _ventanas(texto):
                # Los PDFs de guias clinicas repiten encabezados, pies de pagina
                # y tablas completas. Indexar el mismo texto varias veces infla el
                # indice y deja que un mismo parrafo cope los cinco resultados.
                huella = sha256_texto(normalizar(trozo))
                if huella not in vistos:
                    vistos.add(huella)
                    chunks.append(
                        ChunkIngerido(
                            # `orden` entra en el id para garantizar unicidad sin
                            # perder determinismo: la misma ingesta del mismo PDF
                            # produce siempre los mismos ids.
                            chunk_id=sha256_texto(f"{doc_id}:{pagina.numero}:{orden}:{trozo}")[:40],
                            doc_id=doc_id,
                            orden=orden,
                            pagina=pagina.numero,
                            texto=trozo,
                            n_tokens=max(1, len(trozo) // 4),
                        )
                    )
                    orden += 1

    return chunks


def _ventanas(texto: str) -> list[str]:
    """Corta en limites de frase, con solape."""

    if len(texto) <= TAM_CHUNK:
        salida = [texto]
    else:
        frases = re.split(r"(?<=[.!?])\s+", texto)
        salida = []
        actual: list[str] = []
        largo = 0
        for frase in frases:
            actual.append(frase)
            largo += len(frase) + 1
            if largo >= TAM_CHUNK:
                salida.append(" ".join(actual).strip())
                # Solape: se reinicia arrastrando las ultimas frases.
                arrastre: list[str] = []
                acumulado = 0
                for f in reversed(actual):
                    if acumulado >= SOLAPE_CHUNK:
                        break
                    arrastre.insert(0, f)
                    acumulado += len(f) + 1
                actual = arrastre
                largo = acumulado
        resto = " ".join(actual).strip()
        if len(resto) >= MIN_CHUNK:
            salida.append(resto)
        elif salida and resto:
            salida[-1] = (salida[-1] + " " + resto).strip()

    return [s for s in salida if s]
