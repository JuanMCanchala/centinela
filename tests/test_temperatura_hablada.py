"""Cómo dice una persona su temperatura, y el falso negativo que faltaba.

Este archivo existe por un hallazgo con consecuencia clinica. `"treinta y siete y medio"`
se leia **37.0** en vez de 37.5, y ese medio grado cruza el umbral de febricula
(`FIEBRE_AMARILLO_C = 37.4`) en la direccion mala: el paciente reportaba febricula y el
sistema anotaba temperatura normal.

Los 160 casos oficiales no podian encontrarlo. Vienen con la cifra ya escrita
(`"38.5"`), asi que solo ejercitan el camino del decimal en digitos. El error vivia en el
camino de las palabras, que es el unico que existe cuando alguien **habla**.

"y medio" no es una forma rara: dicho por telefono es probablemente la mas comun de
todas. Lo mismo "con cinco".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from centinela.clinical.normalizer import normalizar_turno  # noqa: E402
from centinela.clinical.thresholds import FIEBRE_AMARILLO_C, FIEBRE_ROJO_C  # noqa: E402


def temp(texto: str):
    return normalizar_turno(texto, "fiebre").numeros.temperatura_c


# ---------------------------------------------------------------- el fallo que motivo esto

def test_y_medio_no_se_pierde() -> None:
    """El caso exacto del hallazgo: medio grado que cambia el nivel clinico."""

    assert temp("treinta y siete y medio") == 37.5


def test_ese_medio_grado_es_la_diferencia_entre_febricula_y_normal() -> None:
    """La razon por la que el fallo importaba, escrita como comprobacion."""

    assert temp("treinta y siete y medio") >= FIEBRE_AMARILLO_C
    assert 37.0 < FIEBRE_AMARILLO_C, "si esto cambia, el test de arriba deja de significar"


def test_y_medio_sobre_una_temperatura_roja() -> None:
    assert temp("tengo treinta y ocho y medio") == 38.5
    assert temp("tengo treinta y ocho y medio") >= FIEBRE_ROJO_C


# ---------------------------------------------------------------- las tres formas del decimal

@pytest.mark.parametrize("dicho,esperado", [
    # marcado con punto o coma
    ("treinta y ocho punto cinco", 38.5),
    ("treinta y siete coma cuatro", 37.4),
    # marcado con "con", que faltaba
    ("treinta y ocho con cinco", 38.5),
    ("treinta y siete con dos", 37.2),
    # "y medio" y "y pico", que faltaban los dos en letra
    ("treinta y ocho y medio", 38.5),
    ("treinta y nueve y medio", 39.5),
    ("tenia treinta y siete y pico", 37.5),
    ("treinta y ocho y pico", 38.5),
    # suelto tras el entero, que es como lo dice un paciente colombiano
    ("treinta y siete cinco", 37.5),
    ("treinta y siete cuatro", 37.4),
    # entero pelado
    ("treinta y ocho", 38.0),
    ("treinta y siete", 37.0),
    # en cifras, los dos separadores
    ("38.5", 38.5),
    ("38,5", 38.5),
    ("37 y pico", 37.5),
])
def test_las_formas_de_decir_una_temperatura(dicho: str, esperado: float) -> None:
    assert temp(dicho) == esperado


def test_dentro_de_una_frase_entera() -> None:
    """Nadie contesta con el numero pelado."""

    assert temp("me tomé la temperatura y estaba en treinta y siete cinco") == 37.5
    assert temp("pues anoche tenía treinta y ocho y medio, me asusté") == 38.5


# ---------------------------------------------------------------- lo que NO debe pasar

def test_no_se_inventa_una_temperatura_donde_no_la_hay() -> None:
    assert temp("no me la he tomado") is None
    assert temp("me duele bastante") is None


def test_un_numero_fuera_del_rango_humano_no_es_una_temperatura() -> None:
    """La guarda de rango: 34-43 grados. Un "cuarenta y cinco" es otra cosa."""

    assert temp("cuarenta y cinco") is None
    assert temp("treinta y dos") is None


# ==========================================================================
# Como describe un paciente su herida
#
# Mismo tipo de hueco que el de la temperatura: expresiones muy comunes que las reglas
# no cubrian y que caian al modelo -- 2.2 s de espera -- y que con el modelo caido se
# perdian enteras. Medido antes del arreglo, ninguna de estas producia pista:
# "la herida se ve roja e hinchada", "se ve inflamada", "esta hinchada alrededor".
# ==========================================================================

def pista_herida(texto: str):
    return normalizar_turno(texto, "herida").pistas.get("herida")


@pytest.mark.parametrize("dicho", [
    "la herida se ve roja e hinchada",
    "se ve inflamada",
    "esta hinchada alrededor",
    "tiene mucha hinchazon",
    "la herida esta muy roja",
    "la herida esta un poco roja",
    "la herida se ve enrojecida",
])
def test_los_signos_inflamatorios_locales_los_pillan_las_reglas(dicho: str) -> None:
    """Sin modelo. La categoria es la amarilla: inflamacion no es purulencia."""

    assert pista_herida(dicho) == "eritema_leve"


def test_la_secrecion_purulenta_sigue_ganando() -> None:
    """Lo rojo no puede tapar lo purulento, que es la bandera roja."""

    assert pista_herida("la herida esta roja y botando pus") == "secrecion_purulenta"


def test_una_herida_normal_sigue_siendo_normal() -> None:
    assert pista_herida("la herida se ve bien") == "normal"
    assert pista_herida("esta cicatrizando bien") == "normal"


def test_rojo_fuera_de_la_herida_no_es_un_hallazgo() -> None:
    """Por eso "roja" suelta no entra en el lexico: hace falta el contexto."""

    assert pista_herida("la pastilla roja no me la tome") is None
    assert pista_herida("tengo la camisa roja") is None


# ==========================================================================
# La temperatura deletreada: lo que Whisper ESCRIBE, no lo que el paciente dice
#
# En una llamada por voz real, el paciente dijo "treinta y siete cinco" y la
# transcripcion fue "Tres, siete, cinco.". El normalizador no lo reconocia, asi que el
# turno no producia temperatura: el dato existia, se oyo bien, y se perdia al escribirlo.
# ==========================================================================

@pytest.mark.parametrize("transcrito,esperado", [
    ("Tres, siete, cinco.", 37.5),
    ("Tres, ocho, cinco.", 38.5),
    ("tres siete cinco", 37.5),
    ("Tres, nueve, cero.", 39.0),
])
def test_la_temperatura_deletreada_se_entiende(transcrito: str, esperado: float) -> None:
    """Con el agente preguntando por la fiebre, que es cuando esto puede aparecer."""

    assert temp(transcrito) == esperado


def test_tres_digitos_sueltos_no_son_una_temperatura_sin_contexto() -> None:
    """La guarda que hace seguro el patron: fuera del dominio de fiebre no se aplica.

    Tres digitos deletreados son demasiadas otras cosas -- una dosis, un telefono, una
    direccion -- para aceptarlos sin que nadie haya hablado de temperatura.
    """

    assert normalizar_turno("Tres, siete, cinco.", "dolor").numeros.temperatura_c is None
    assert normalizar_turno("tres siete cinco", "").numeros.temperatura_c is None


def test_el_contexto_puede_venir_del_propio_turno() -> None:
    """No hace falta que el agente pregunte: basta con que el paciente lo nombre."""

    n = normalizar_turno("la temperatura fue tres siete cinco", "")

    assert n.numeros.temperatura_c == 37.5
