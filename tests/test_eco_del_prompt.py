"""Dos agujeros del filtro de alucinaciones, encontrados al mover el STT a la GPU.

**Uno: el prompt clinico devuelto como si lo hubiera dicho el paciente.**

Es el modo de alucinacion que las dos puertas de confianza no pueden parar, y la razon
es incomoda: el decoder esta *seguro* del texto que escribe, porque lo esta copiando de
su propio `initial_prompt`. `no_speech_prob` bajo y `avg_logprob` alto son exactamente
lo que se espera de una copia. Medido con `medium` sobre ventanas de 0.64 s de las
grabaciones de `eval/audios`, dos de dieciocho salieron asi y ninguna se descarto:

    01_si_soy_yo             -> "Llamada de seguimiento postoperatorio en Colombia."
    07_treinta_y_siete_cinco -> "El paciente responde sobre dolor, ..."

Y es tambien lo que produjo el unico corte falso de `eval/bargein.py` a -6 dB. Lo que
cuesta en cada camino: en el del turno, ese texto va a la extraccion clinica; en el del
barge-in, confirma una interrupcion que nadie hizo y le corta la palabra al agente.

El filtro viejo no podia verlo porque persigue frases de YouTube -- "suscribete",
"gracias por ver" --, no el contexto que le pasamos nosotros.

**Dos: "no" y "si" se descartaban como texto vacio.**

`es_alucinacion` empezaba con `len(base) < 3`. `normalizar` conserva el punto final, asi
que "No." son tres caracteres y pasaba, y "No" son dos y se descartaba. Las dos
respuestas mas consecuentes de un cuestionario clinico dependian de un signo de
puntuacion que el decoder no esta obligado a poner. El guion de `eval/escucha.py` lo
dice de `02_no.wav`: "la respuesta mas corta posible; si esta se pierde, se pierde medio
cuestionario".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from centinela.stt.whisper import (  # noqa: E402
    MIN_CARACTERES_ECO_PROMPT,
    PROMPT_CLINICO,
    _INSTRUCCIONES_DEL_PROMPT,
    es_alucinacion,
    es_eco_del_prompt,
    normalizar,
)


# ==========================================================================
# 1. El prompt devuelto se descarta
# ==========================================================================

ECOS = (
    "Llamada de seguimiento postoperatorio en Colombia.",
    "El paciente responde sobre dolor, ",
    "El paciente responde sobre dolor, fiebre, temperatura, movilidad",
    " llamada de seguimiento postoperatorio en colombia",
    PROMPT_CLINICO,
)


@pytest.mark.parametrize("texto", ECOS)
def test_el_prompt_devuelto_se_descarta(texto: str) -> None:
    assert es_alucinacion(texto) == "el prompt clinico devuelto"


def test_se_detecta_en_los_dos_sentidos() -> None:
    """El prompt entero dentro de un turno, y un trozo del prompt como turno entero.

    Las dos formas se observaron. "el paciente responde sobre dolor" es un prefijo de la
    segunda frase del prompt y no una frase completa de nada, asi que buscar el patron
    dentro del texto -- como hace el filtro de YouTube -- no lo habria visto.
    """

    instruccion = normalizar(_INSTRUCCIONES_DEL_PROMPT)

    assert es_eco_del_prompt(instruccion), "el prompt completo no se reconoce"
    assert es_eco_del_prompt(instruccion[:40]), "un trozo del prompt no se reconoce"


# ==========================================================================
# 2. Y no se lleva por delante lo que el paciente si dice
#
# El prompt trae "Ejemplos: si senora, no he tenido fiebre, ..." a proposito: son
# frases reales de paciente. Derivar los patrones de esa mitad convertiria el filtro
# en un descartador de respuestas legitimas, que es peor que no tenerlo.
# ==========================================================================

RESPUESTAS_REALES = (
    "No he tenido fiebre",
    "no he tenido fiebre, nada",
    "el dolor esta como en un seis",
    "la herida se ve enrojecida",
    "tengo treinta y siete cinco de temperatura",
    "Si senora",
    "Me duele mucho la herida quirurgica",
    "No puedo caminar ni apoyar el pie",
    "la herida se ve con liquido amarillo y huele feo",
    "dolor",
    "fiebre",
    "el dolor y la fiebre",
)


@pytest.mark.parametrize("texto", RESPUESTAS_REALES)
def test_una_respuesta_real_no_es_eco_del_prompt(texto: str) -> None:
    assert es_alucinacion(texto) is None, f"se descarto una respuesta legitima: {texto!r}"


def test_los_ejemplos_del_prompt_quedan_fuera_de_los_patrones() -> None:
    """La guarda estructural: si alguien deriva los patrones del prompt entero, esto cae."""

    assert "Ejemplos:" in PROMPT_CLINICO, (
        "el prompt ya no tiene la marca 'Ejemplos:' que separa las dos mitades"
    )
    assert "Ejemplos:" not in _INSTRUCCIONES_DEL_PROMPT
    assert "no he tenido fiebre" not in normalizar(_INSTRUCCIONES_DEL_PROMPT)


def test_una_coincidencia_corta_no_basta() -> None:
    """"dolor" esta en el prompt y tambien es una respuesta. La longitud es la que decide."""

    assert not es_eco_del_prompt("dolor")
    assert not es_eco_del_prompt("fiebre temperatura")
    corto = normalizar(_INSTRUCCIONES_DEL_PROMPT)[: MIN_CARACTERES_ECO_PROMPT - 2]
    assert not es_eco_del_prompt(corto)


def test_los_patrones_se_derivan_del_prompt_y_no_se_copian() -> None:
    """Si el prompt cambia, el filtro cambia con el. Una copia se habria desincronizado."""

    assert _INSTRUCCIONES_DEL_PROMPT
    assert PROMPT_CLINICO.startswith(_INSTRUCCIONES_DEL_PROMPT)


# ==========================================================================
# 3. "no" y "si" sobreviven, y la basura no
# ==========================================================================

@pytest.mark.parametrize("texto", ["No", "No.", "no", "Si", "Sí", "sí", "Ya", "Ya."])
def test_las_respuestas_de_una_palabra_no_son_texto_vacio(texto: str) -> None:
    """Con la regla vieja, las versiones sin punto se descartaban."""

    assert es_alucinacion(texto) is None, (
        f"{texto!r} se descarto: es la respuesta mas corta del cuestionario"
    )


@pytest.mark.parametrize("texto", ["", " ", ".", "...", "-", "!", "?", ". . .", "¿?"])
def test_un_turno_sin_contenido_sigue_siendo_texto_vacio(texto: str) -> None:
    assert es_alucinacion(texto) == "texto vacio"


def test_el_guion_de_escucha_sigue_teniendo_la_respuesta_de_una_palabra() -> None:
    """Que la grabacion que motiva todo esto siga en el guion, con su razon escrita."""

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from eval.escucha import GUION

    cortas = [f for f in GUION if len(f.dice.split()) == 1]
    assert cortas, "el guion perdio las respuestas de una palabra"
    assert any(f.archivo == "02_no.wav" for f in GUION)
