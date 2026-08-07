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
import re
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
from centinela.rag.ingest import normalizar  # noqa: E402
from centinela.rag.store import KnowledgeStore  # noqa: E402

DESTINO = RAIZ / "data" / "threshold_citations.json"

# Puntaje minimo de senal clinica para que una frase valga como respaldo.
MIN_PUNTAJE_CITA = 2.5


def cumple_terminos(frase: str, grupos: tuple[tuple[str, ...], ...]) -> bool:
    """La frase satisface al menos un grupo de terminos requeridos.

    Dentro de un grupo hay conjuncion (todos los terminos), entre grupos hay
    disyuncion (basta uno). Asi se expresa "fiebre Y 38" o alternativamente
    "temperatura Y 38" sin escribir una expresion regular ilegible.
    """

    base = normalizar(frase)
    satisface = False
    for grupo in grupos:
        if all(normalizar(t) in base for t in grupo):
            satisface = True
    return satisface


def buscar_respaldo(umbral, chunks: list[dict]) -> tuple[dict, str, float] | None:
    """Encuentra la frase del corpus que sustenta un umbral.

    Historia de este algoritmo, porque explica su forma actual:

    **Intento 1: similitud semantica sobre chunks.** Devolvia pasajes con
    similitud 0.88 que no decian nada del umbral -- la portada de un PDF, un
    aviso de licencia Creative Commons, un indice de contenidos. Alta similitud
    con la *consulta* no es lo mismo que sustentar la *afirmacion*.

    **Intento 2: terminos requeridos + solape lexico con la consulta.** Mejoro
    las citas en espanol, pero descartaba las mejores. El caso que lo demostro:
    para la regla de secrecion purulenta, la mejor frase del corpus es
    "Supuracion con pus, mal olor," -- de una guia *para pacientes* de reemplazo
    de cadera, en la lista de cuando llamar al medico. Solape lexico con la
    consulta: 0.000, porque una guia para pacientes no dice "secrecion purulenta
    en sitio operatorio", dice "pus" y "mal olor". Puntuar por solape con mi
    propia consulta premiaba el lenguaje academico y castigaba justo el lenguaje
    que un paciente reconoce.

    **Version actual:** el filtro de admision son los `terminos_requeridos`, que
    son especificos del umbral. Entre las frases que pasan, se puntua por
    **densidad de senal clinica**: cuantos terminos de alarma contiene y si esta
    en un contexto de signos de alarma o de instrucciones al paciente. El solape
    con la consulta ya no interviene.
    """

    mejor: tuple[dict, str, float] | None = None

    for chunk in chunks:
        frases = partir_frases(chunk["texto"])
        for i, frase in enumerate(frases):
            # Minimo de 24 caracteres: las guias usan frases telegraficas en las
            # listas de signos de alarma ("Supuracion con pus, mal olor,") y esas
            # son precisamente las mas relevantes para citarle a un paciente.
            if 24 <= len(frase) <= 420 and cumple_terminos(frase, umbral.terminos_requeridos):
                puntaje = puntuar_cita(frase, frases, i, chunk)
                if mejor is None or puntaje > mejor[2]:
                    mejor = (chunk, frase, puntaje)

    if mejor is not None and mejor[2] < MIN_PUNTAJE_CITA:
        mejor = None

    return mejor


# Vocabulario que indica que la frase esta enunciando un signo de alarma o una
# instruccion al paciente, que es el contexto donde un umbral clinico de verdad
# se sustenta. No es lenguaje de resultados de estudio.
SENAL_ALARMA = (
    "signo", "alarma", "alerta", "consulte", "consultar", "acuda", "acudir",
    "urgencias", "llame", "llamar", "avise", "inmediato", "inmediata",
    "atencion medica", "vigilar", "vigilancia", "pendiente", "si presenta",
    "si nota", "si aparece", "cuando llamar", "warning", "seek", "contact",
    "emergency", "immediately", "should call", "report",
)

SENAL_INSTRUCCION_PACIENTE = (
    "usted", "su herida", "su medico", "en casa", "cuidado", "recomendacion",
    "debe", "no debe", "evite", "importante",
)


def puntuar_cita(frase: str, frases: list[str], indice: int, chunk: dict) -> float:
    """Densidad de senal clinica de una frase candidata.

    Puntua alto lo que un clinico reconoceria como el enunciado de un criterio, y
    bajo lo que es prosa de resultados. La ventana de contexto importa: en una
    lista de signos de alarma, el encabezado esta una o dos frases antes.
    """

    base = normalizar(frase)
    ventana = normalizar(" ".join(frases[max(0, indice - 4):indice + 2]))

    alarma_en_frase = sum(1 for t in SENAL_ALARMA if t in base)
    alarma_en_contexto = sum(1 for t in SENAL_ALARMA if t in ventana)
    instruccion = sum(1 for t in SENAL_INSTRUCCION_PACIENTE if t in ventana)

    puntaje = 0.0
    puntaje += 3.0 * min(alarma_en_frase, 3)
    puntaje += 1.0 * min(alarma_en_contexto, 4)
    puntaje += 0.5 * min(instruccion, 4)

    # Bono grande al encabezado de una lista de signos de alarma. Es el contexto
    # de maxima calidad para citar: el documento esta enunciando explicitamente
    # cuando el paciente debe buscar atencion, que es exactamente lo que el motor
    # de decision hace. Sin este bono, el algoritmo prefiere tablas de escalas
    # clinicas y frases de resultados, que contienen el termino pero no enuncian
    # ningun criterio de actuacion.
    for encabezado in ("signos de alarma", "signos de infeccion", "cuando llamar",
                       "consulte a su medico", "acuda a urgencias", "llame a su medico",
                       "motivos de consulta", "cuando consultar", "senales de alerta",
                       "when to call", "warning signs", "seek medical"):
        if encabezado in ventana:
            puntaje += 6.0

    # Una frase corta y concreta se verifica mejor que un parrafo: el jurado la
    # busca con Ctrl+F en el PDF.
    puntaje += max(0.0, 2.0 - len(frase) / 100)

    # Penalizacion al lenguaje de resultados de estudio y a las tablas: describen
    # hallazgos de una cohorte o listan variables, no enuncian un criterio.
    for t in ("incidencia", "prevalencia", "cohorte", "pacientes fueron",
              "se incluyeron", "odds ratio", "intervalo de confianza", "p <",
              "n =", "tabla", "figura", "grafico", "analisis estadistico",
              "underwent", "were included", "retrospective", "escala kss",
              "variable", "independiente", "puntaje total", "desviacion estandar"):
        if t in ventana:
            puntaje -= 2.0

    return puntaje


def partir_frases(texto: str) -> list[str]:
    return [f.strip() for f in re.split(r"(?<=[.!?;])\s+|\n", texto) if f.strip()]


def main() -> int:
    store = KnowledgeStore(RAIZ / "data" / "index")
    stats = store.estadisticas()
    if stats["documentos"] == 0:
        print("el indice esta vacio; corre `make index` primero")
        return 2

    print(f"corpus: {stats['documentos']} documentos, {stats['chunks']} fragmentos")

    citas: dict[str, dict] = {}
    sin_respaldo: list[str] = []

    todos = [("rojo", u) for u in UMBRALES_ROJOS] + [("amarillo", b) for b in BANDERAS_AMARILLAS]

    chunks = store.chunks_activos()
    print(f"buscando entre {len(chunks)} fragmentos\n")

    for clase, u in todos:
        print(f"[{clase:8s}] {u.codigo:14s} {u.umbral_legible}")
        elegido = buscar_respaldo(u, chunks)

        if elegido is None:
            print("             SIN RESPALDO en el corpus para este umbral")
            print("             (se reporta como no resuelto; no se cita nada dudoso)")
            sin_respaldo.append(u.codigo)
        else:
            chunk, frase, puntaje = elegido
            doc = store.obtener_documento(chunk["doc_id"])
            citas[u.codigo] = {
                "documento": chunk["nombre"],
                "documento_sha256": doc.sha256 if doc else None,
                "pagina": chunk["pagina"],
                "cita_textual": frase,
                "puntaje": round(puntaje, 4),
            }
            print(f"             -> {chunk['nombre'][:66]}")
            print(f"                pag. {chunk['pagina']}   senal {puntaje:.1f}")
            print(f"                \"{frase[:190]}\"")
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
