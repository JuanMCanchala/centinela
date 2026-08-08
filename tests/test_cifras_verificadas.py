"""Que una cifra de la respuesta signifique lo mismo que en el corpus.

La verificacion posterior comprobaba que cada cifra de la respuesta apareciera en el
contexto recuperado. Es la comprobacion correcta y es demasiado debil, porque un
numero que aparece en cualquier parte y por cualquier motivo le da licencia al modelo
para usarlo en cualquier sentido.

Se vio fallar: a "cuando me quitan los puntos" el modelo respondio "a los 15 dias" y
la verificacion lo acepto porque el "15" existia en el contexto -- en *"la puntuacion
del WOMAC disminuyo en 15 puntos"*, de un articulo de rodilla. La cifra estaba; el
plazo era invencion. Y en salud el dato no es el numero, es el numero con su unidad:
una dosis, un umbral, un plazo.

La otra mitad de estas pruebas es igual de importante y menos obvia: **rechazar de mas
tampoco es inocuo**. Endurecer la comprobacion degrada respuestas correctas a cita
literal, en silencio, y eso empeora el sistema sin que salte ninguna alarma. La primera
version de la regex solo aceptaba el signo de grado (U+00B0) y el corpus entregado
escribe "38ºc" con el INDICADOR ORDINAL (U+00BA), asi que marcaba como inventada una
temperatura que estaba literalmente en la guia.

Los dos lados se prueban aqui, con las grafias que el corpus usa de verdad.
"""

from __future__ import annotations

import pytest

from centinela.rag.answerer import _cifras_con_unidad, _limpiar_para_hablar

# (nombre, lo que dice la respuesta, lo que dice el contexto)
EQUIVALENTES = [
    ("signo de grado contra indicador ordinal", "mas de 38 °C", "Fiebre > 38ºc"),
    ("indicador ordinal en los dos", "superior a 38 ºC", "(temperatura superior a 38 ºC)."),
    ("grados escrito con palabras", "38 grados", "fiebre de 38ºC"),
    ("grados centigrados", "mas de 38 grados centigrados", "fiebre mayor a 38 ºC"),
    ("el corpus lo escribe en letra", "a los 8 dias", "retire los puntos a los ocho dias"),
    ("la respuesta lo escribe en letra", "en ocho dias", "control a los 8 dias"),
    ("singular contra plural", "1 semana", "una semana despues"),
    ("decimal con coma y con punto", "38.5 grados", "fiebre de 38,5 ºC"),
]


@pytest.mark.parametrize("nombre, respuesta, contexto", EQUIVALENTES)
def test_una_cifra_del_corpus_no_se_marca_por_como_esta_escrita(
    nombre: str, respuesta: str, contexto: str
) -> None:
    """Rechazar de mas degrada respuestas correctas, y lo hace sin avisar."""

    sobrantes = _cifras_con_unidad(respuesta) - _cifras_con_unidad(contexto)
    assert sobrantes == set(), f"{nombre}: {respuesta!r} contra {contexto!r}"


# La misma cifra midiendo otra cosa. Es el fallo que la comprobacion anterior no veia.
DISTINTOS = [
    ("puntos de una escala leidos como dias", "a los 15 dias", "el WOMAC disminuyo en 15 puntos"),
    ("dias leidos como semanas", "6 semanas de reposo", "reposo de 6 dias"),
    ("horas leidas como dias", "durante 24 dias", "durante 24 horas"),
    ("una temperatura que no esta", "fiebre de 39 grados", "fiebre mayor a 38 ºC"),
    ("un plazo que no esta", "en 10 dias", "control al mes"),
]


@pytest.mark.parametrize("nombre, respuesta, contexto", DISTINTOS)
def test_una_cifra_usada_con_otra_unidad_si_se_marca(
    nombre: str, respuesta: str, contexto: str
) -> None:
    sobrantes = _cifras_con_unidad(respuesta) - _cifras_con_unidad(contexto)
    assert sobrantes, f"{nombre}: {respuesta!r} deberia marcarse contra {contexto!r}"


def test_el_caso_exacto_que_motivo_la_comprobacion() -> None:
    """El WOMAC de rodilla convertido en plazo de retirada de puntos."""

    contexto = (
        "Niveles de dolor, rigidez y funcionalidad: la mediana de puntuacion en dolor "
        "del WOMAC disminuyo en 15 puntos, para puntuacion total."
    )
    respuesta = "Los puntos suelen retirarse a los 15 dias postoperatorio."

    # La comprobacion vieja pasaba: el "15" esta en el contexto.
    assert "15" in contexto
    # La nueva no.
    assert ("15", "dia") in _cifras_con_unidad(respuesta) - _cifras_con_unidad(contexto)


# ---------------------------------------------------------------------------
# La cita que se le lee al paciente
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cuando_se_descarta_lo_generado_se_le_lee_el_corpus() -> None:
    """El nivel intermedio: cita literal antes de abstenerse.

    Antes, un texto generado que fallaba la verificacion se convertia en "no tengo esa
    informacion" aunque el corpus SI tuviera la respuesta y estuviera recuperada. El
    paciente se quedaba sin nada por un defecto que no era del corpus.
    """

    from centinela.rag.answerer import ResponderClinico
    from centinela.rag.retriever import Pasaje

    pasaje = Pasaje(
        chunk_id="c1", doc_id="d1", nombre="Guia de colecistectomia.pdf", pagina=4,
        texto=(
            "Consulte de inmediato si presenta fiebre mayor a 38 ºC o secrecion "
            "purulenta en la herida quirurgica."
        ),
        tema="colecistitis", similitud=0.91,
    )

    class RecuperadoFalso:
        fundamentado = True
        razon = "prueba"
        generacion = 1
        pasajes = [pasaje]

    class RetrieverFalso:
        class store:
            @staticmethod
            def obtener_documento(_):
                return None

        @staticmethod
        def recuperar(*_a, **_k):
            return RecuperadoFalso()

    class LLMQueInventa:
        @staticmethod
        async def generar(**_k):
            from centinela.llm.backend import UsoTokens

            class Generada:
                texto = "Debe consultar si la fiebre pasa de 39 grados."
                uso = UsoTokens()

            return Generada()

    responder = ResponderClinico(RetrieverFalso(), LLMQueInventa())
    r = await responder.responder("¿cuando debo ir a urgencias?")

    # El 39 no esta en el corpus: el texto generado se descarta.
    assert r.verificaciones_falladas, "la verificacion tenia que rechazar el 39"
    # Y en vez de callar, se le lee la frase de la guia.
    assert r.extractiva is True
    assert r.fundamentado is True
    assert "38" in r.texto and "39" not in r.texto
    assert r.citas and r.citas[0]["pagina"] == 4
    # La cita textual es literal, para que se pueda buscar en el PDF.
    assert r.citas[0]["cita_textual"] in pasaje.texto


def test_la_cita_hablada_no_arrastra_los_numeros_de_pagina_del_ocr() -> None:
    """Un numero de pagina leido en voz alta suena como un dato clinico.

    El OCR del corpus deja el numero de pagina en medio de la frase. La `cita_textual`
    del informe conserva el texto literal -- es lo que se busca con Ctrl+F en el PDF --
    y lo que se dice por telefono va limpio.
    """

    crudo = "Cuando tenga:\n17\nFiebre (temperatura superior a 38 ºC)."

    hablada = _limpiar_para_hablar(crudo)

    assert "17" not in hablada
    assert hablada == "Cuando tenga: Fiebre (temperatura superior a 38 ºC)"
