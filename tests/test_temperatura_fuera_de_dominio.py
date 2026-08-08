"""La temperatura se acepta aunque el agente estuviera preguntando otra cosa.

El fallo que motiva estas pruebas se vio en una llamada real: el agente preguntaba
por el sueno, el paciente contestaba con su temperatura, y el dato se perdia. La
extraccion solo miraba la temperatura si el turno hablaba de fiebre o si la fiebre
era el dominio preguntado, asi que un numero dicho a destiempo no producia nada.

Dentro de este cuestionario un numero entre 35 y 42 no puede ser otra cosa: la
escala de dolor va de 0 a 10 y ningun otro dominio produce cifras. Lo unico que
compite son las magnitudes que se dicen con las mismas cifras -- la edad, los
minutos, el peso -- y esas se distinguen por la unidad que va detras del numero.

Ese es el contrato que se prueba aqui, por los dos lados: lo que ahora si se
captura, y lo que sigue sin capturarse.
"""

from __future__ import annotations

import pytest

from centinela.clinical.normalizer import extraer_numeros

# ---------------------------------------------------------------------------
# Lo que ahora SI se captura
# ---------------------------------------------------------------------------

# Cada caso es (lo que dijo el paciente, el dominio que el agente pregunto,
# la temperatura esperada). El dominio nunca es "fiebre": el punto es justamente
# que la fiebre no se estaba preguntando.
FUERA_DE_DOMINIO = [
    ("treinta y ocho", "sueno", 38.0),
    ("Treinta y siete cinco", "dolor", 37.5),
    ("38", "apetito", 38.0),
    ("38.5", "movilidad", 38.5),
    ("Pues anoche marco 39", "herida", 39.0),
    ("Estaba en 37,9 anoche", "apetito", 37.9),
    ("me marco treinta y nueve", "movilidad", 39.0),
    ("37 y pico", "sueno", 37.5),
]


@pytest.mark.parametrize("dice, pregunta, espera", FUERA_DE_DOMINIO)
def test_la_temperatura_se_captura_aunque_se_preguntara_otro_dominio(
    dice: str, pregunta: str, espera: float
) -> None:
    n = extraer_numeros(dice, pregunta)
    assert n.temperatura_c == espera, f"{dice!r} preguntando por {pregunta}"
    assert n.temperatura_fuera_de_dominio is True


def test_la_marca_de_fuera_de_dominio_no_se_pone_cuando_si_habia_contexto() -> None:
    """La marca sirve para la traza, asi que tiene que ser exacta en los dos casos."""

    con_contexto = extraer_numeros("tenia 38 de fiebre", "sueno")
    assert con_contexto.temperatura_c == 38.0
    assert con_contexto.temperatura_fuera_de_dominio is False

    preguntada = extraer_numeros("38", "fiebre")
    assert preguntada.temperatura_c == 38.0
    assert preguntada.temperatura_fuera_de_dominio is False


def test_una_bandera_roja_de_fiebre_no_se_pierde_por_llegar_a_destiempo() -> None:
    """Es el caso que da valor a todo lo anterior.

    39 grados cumple `R1_FIEBRE`. Si el paciente lo suelta mientras el agente
    pregunta por la herida y el dato se descarta, el motor decide sin el criterio
    de alarma que ya se habia cumplido.
    """

    n = extraer_numeros("ah y ayer me marco 39", "herida")
    assert n.temperatura_c == 39.0


# ---------------------------------------------------------------------------
# Lo que NO se captura: la misma cifra midiendo otra cosa
# ---------------------------------------------------------------------------

OTRA_MAGNITUD = [
    "Tengo 38 anos",
    "tengo treinta y ocho anos",
    "hace 40 minutos",
    "como hace 35 minutos que me tome la pastilla",
    "llevo 40 dias asi",
    "peso 40 kilos",
    "van 36 horas sin dormir",
    "me tome 40 gotas",
    "vivo a 40 cuadras",
    "tengo 42 anos y me operaron la semana pasada",
]


@pytest.mark.parametrize("dice", OTRA_MAGNITUD)
def test_una_cifra_con_otra_unidad_no_es_una_temperatura(dice: str) -> None:
    n = extraer_numeros(dice, "sueno")
    assert n.temperatura_c is None, f"{dice!r} se leyo como temperatura"


# Dos cifras en el mismo turno, la primera midiendo otra cosa. La guarda descarta
# el numero que lleva unidad detras, no el turno completo -- y con `search` a secas
# se perdia la temperatura de verdad porque nadie seguia buscando tras el descarte.
DOS_CIFRAS = [
    ("tengo 38 anos y la temperatura me marco 39.2", 39.2),
    ("tengo 38 anos y me marco 39", 39.0),
    ("peso 40 kilos y tenia 38", 38.0),
    ("hace 35 minutos me marco 39", 39.0),
    ("tengo treinta y ocho anos y me marco treinta y nueve", 39.0),
]


@pytest.mark.parametrize("dice, espera", DOS_CIFRAS)
def test_una_magnitud_descartada_no_tapa_la_temperatura_del_mismo_turno(
    dice: str, espera: float
) -> None:
    n = extraer_numeros(dice, "sueno")
    assert n.temperatura_c == espera, f"{dice!r}"


# ---------------------------------------------------------------------------
# Que no se rompa lo que ya funcionaba
# ---------------------------------------------------------------------------

def test_el_dolor_sigue_leyendose_y_no_se_confunde_con_la_temperatura() -> None:
    n = extraer_numeros("Un seis", "dolor")
    assert n.dolor_nrs == 6
    assert n.temperatura_c is None


def test_un_turno_con_temperatura_y_dolor_resuelve_los_dos() -> None:
    n = extraer_numeros("el dolor como un 4 y la fiebre en 38.2", "dolor")
    assert n.dolor_nrs == 4
    assert n.temperatura_c == 38.2
