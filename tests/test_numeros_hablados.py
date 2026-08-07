"""Los numeros como los dice un paciente por telefono.

Dos huecos que rompian la conversacion, los dos encontrados con una llamada de voz
completa (`eval/conversacion_voz.py`) y ninguno visible en una prueba de un turno.

**Numero suelto.** El agente pregunta *"en que numero pondria el dolor, de cero a
diez?"* y el paciente responde *"Un seis"*. Ese turno no contiene ninguna palabra
del dominio: el contexto esta en la pregunta, no en la respuesta. El sistema no
extraia nada, el dolor quedaba sin resolver, y el agente repetia la pregunta. En un
cuestionario clinico ese es el caso mas frecuente de todos.

**Numeros en palabras.** Whisper escribe los numeros en letra a menudo y casi
siempre en turnos cortos. Un paciente colombiano dice la temperatura como *"treinta
y siete cinco"*, y eso no lo veia ninguna expresion regular de digitos.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from centinela.clinical.normalizer import extraer_numeros, normalizar_turno  # noqa: E402

# ------------------------------------------------------------------
# Numero suelto cuando se pregunto por el dolor
# ------------------------------------------------------------------

NUMERO_SUELTO = (
    ("Un seis", 6),
    ("Un seis.", 6),
    ("Seis", 6),
    ("seis", 6),
    ("Como un cuatro", 4),
    ("un 6", 6),
    ("6", 6),
    ("Cero", 0),
    ("Diez", 10),
    ("cinco de diez", 5),
    ("como en un tres", 3),
)


@pytest.mark.parametrize("texto,esperado", NUMERO_SUELTO)
def test_numero_suelto_cuenta_si_se_pregunto_por_el_dolor(texto: str, esperado: int) -> None:
    n = extraer_numeros(texto, dominio_objetivo="dolor")
    assert n.dolor_nrs == esperado, f"{texto!r} deberia dar dolor={esperado}, dio {n.dolor_nrs}"


@pytest.mark.parametrize("texto,esperado", NUMERO_SUELTO)
def test_el_mismo_numero_llega_por_normalizar_turno(texto: str, esperado: int) -> None:
    """El dominio tiene que propagarse hasta aqui, no solo existir en la firma."""

    n = normalizar_turno(texto, dominio_objetivo="dolor")
    assert n.numeros.dolor_nrs == esperado


def test_numero_suelto_sin_dominio_no_se_atribuye() -> None:
    """Sin saber que se pregunto, un numero suelto no significa nada.

    Es la otra cara de la moneda: aceptar "seis" siempre convertiria cualquier
    mencion de un numero en un dato clinico.
    """

    n = extraer_numeros("Seis", dominio_objetivo="")
    assert n.dolor_nrs is None


def test_no_confunde_una_fecha_con_dolor() -> None:
    n = extraer_numeros("me operaron el seis de junio", dominio_objetivo="herida")
    assert n.dolor_nrs is None


def test_no_inventa_cuando_no_hay_numero() -> None:
    for texto in ("no se", "no me acuerdo", "mas o menos igual", "tengo tres hijos"):
        n = extraer_numeros(texto, dominio_objetivo="dolor")
        assert n.dolor_nrs is None, f"invento un numero en {texto!r}: {n.dolor_nrs}"


# ------------------------------------------------------------------
# Temperaturas dichas en palabras
# ------------------------------------------------------------------

TEMPERATURAS = (
    ("me tome la temperatura y estaba en treinta y siete cinco", 37.5),
    ("treinta y siete cinco", 37.5),
    ("treinta y siete punto cinco", 37.5),
    ("tenia treinta y ocho", 38.0),
    ("como treinta y nueve", 39.0),
    ("treinta y seis", 36.0),
    ("cuarenta", 40.0),
    ("estaba en 38.2", 38.2),
    ("marco 37,5", 37.5),
    ("tenia 38 grados", 38.0),
)


@pytest.mark.parametrize("texto,esperado", TEMPERATURAS)
def test_temperatura_en_palabras_o_digitos(texto: str, esperado: float) -> None:
    n = extraer_numeros(texto, dominio_objetivo="fiebre")
    assert n.temperatura_c == pytest.approx(esperado), (
        f"{texto!r} deberia dar {esperado}, dio {n.temperatura_c}"
    )


def test_temperatura_con_contexto_no_necesita_el_dominio() -> None:
    """Si el turno dice "temperatura", no hace falta saber que se pregunto."""

    n = extraer_numeros("me tome la temperatura y estaba en treinta y siete cinco")
    assert n.temperatura_c == pytest.approx(37.5)


def test_fiebre_subjetiva_sin_medicion() -> None:
    n = extraer_numeros("Si, me senti afiebrada pero no me la he tomado",
                        dominio_objetivo="fiebre")
    assert n.temperatura_c is None
    assert n.fiebre_subjetiva is True
    assert n.sin_termometro is True


def test_temperatura_fuera_de_rango_se_ignora() -> None:
    """Un numero absurdo no es una temperatura corporal."""

    n = extraer_numeros("cuarenta y nueve", dominio_objetivo="fiebre")
    assert n.temperatura_c is None


def test_dolor_y_temperatura_en_el_mismo_turno() -> None:
    """No se debe leer el 37 de la temperatura como un dolor de 7."""

    n = extraer_numeros(
        "el dolor es un cinco y la temperatura estaba en treinta y siete cinco",
        dominio_objetivo="dolor",
    )
    assert n.dolor_nrs == 5
    assert n.temperatura_c == pytest.approx(37.5)
