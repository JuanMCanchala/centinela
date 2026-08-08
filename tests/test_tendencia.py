"""Las reglas de tendencia: que disparen con una rampa y que no molesten sin ella.

Contexto que estas pruebas tienen que preservar, porque es lo raro del caso. Sobre las
40 trayectorias oficiales estas dos reglas **no disparan ni una vez** -- el barrido
esta en `eval/tendencia.py` y la razon es que el deterioro del dataset es un escalon,
no una rampa. Se mantienen porque un deterioro real si es una rampa y el coste medido
en precision es exactamente cero.

Una regla que nunca dispara en la evaluacion es indistinguible de una regla decorativa
si nadie prueba que funciona. Eso es lo que se prueba aqui, y en los dos sentidos:

  - con una rampa sintetica, disparan;
  - sin historia, el motor decide exactamente igual que antes de que existieran, que es
    lo que sostiene que `make eval` siga dando 152/160.
"""

from __future__ import annotations

import pytest

from centinela.clinical.tendencia import (
    SALTO_DOLOR_NRS,
    SALTO_FIEBRE_C,
    banderas_de_tendencia,
)
from centinela.clinical.triage_engine import TriageEngine
from centinela.models import ClinicalState, Nivel, Observacion, Procedencia


def estado(dolor: float | None = None, fiebre: float | None = None) -> ClinicalState:
    e = ClinicalState()
    if dolor is not None:
        e.dolor_nrs = Observacion(
            valor=dolor, conocido=True,
            procedencia=Procedencia(turno_idx=1, cita_paciente="prueba"),
        )
    if fiebre is not None:
        e.fiebre_c = Observacion(
            valor=fiebre, conocido=True,
            procedencia=Procedencia(turno_idx=1, cita_paciente="prueba"),
        )
    return e


# La serie tiene la forma que devuelve `EscalationService.serie_por_dominio`:
# dominio -> [(dia, valor como texto), ...]. El valor es texto porque la tabla guarda
# los seis dominios en una sola columna.
def serie(dolor: list[tuple[int, float]] = (), fiebre: list[tuple[int, float]] = ()) -> dict:
    s: dict = {}
    if dolor:
        s["dolor"] = [(d, str(v)) for d, v in dolor]
    if fiebre:
        s["fiebre"] = [(d, str(v)) for d, v in fiebre]
    return s


# ---------------------------------------------------------------------------
# Disparan cuando hay rampa
# ---------------------------------------------------------------------------

def test_el_dolor_que_sube_de_golpe_levanta_la_bandera() -> None:
    hits = banderas_de_tendencia(estado(dolor=8), serie(dolor=[(1, 2), (3, 3)]))

    assert [h.codigo for h in hits] == ["T1_DOLOR_ASCENDENTE"]
    # El valor observado dice de donde viene, no solo donde esta: es lo que hace que la
    # bandera se entienda sin abrir la llamada anterior.
    assert "dia 3: 3.0 -> hoy: 8.0" == hits[0].valor_observado


def test_la_temperatura_que_sube_de_golpe_levanta_la_bandera() -> None:
    hits = banderas_de_tendencia(estado(fiebre=37.9), serie(fiebre=[(1, 36.5), (3, 36.8)]))

    assert [h.codigo for h in hits] == ["T2_FIEBRE_ASCENDENTE"]


def test_una_rampa_en_los_dos_dominios_levanta_las_dos() -> None:
    hits = banderas_de_tendencia(
        estado(dolor=9, fiebre=37.9),
        serie(dolor=[(1, 2), (3, 4)], fiebre=[(1, 36.4), (3, 36.6)]),
    )

    assert sorted(h.codigo for h in hits) == ["T1_DOLOR_ASCENDENTE", "T2_FIEBRE_ASCENDENTE"]


def test_se_compara_contra_la_llamada_MAS_RECIENTE_no_contra_la_primera() -> None:
    """Un paciente que empeoro y luego mejoro no puede quedar marcado para siempre."""

    # dia 1 dolor 2, dia 3 dolor 8, hoy dolor 8: respecto a la mas reciente no hay salto.
    hits = banderas_de_tendencia(estado(dolor=8), serie(dolor=[(1, 2), (3, 8)]))

    assert hits == []


# ---------------------------------------------------------------------------
# No molestan cuando no la hay
# ---------------------------------------------------------------------------

def test_sin_historia_no_hay_banderas_de_tendencia() -> None:
    """Es lo que sostiene que el replay de los 160 casos oficiales no se mueva."""

    assert banderas_de_tendencia(estado(dolor=9, fiebre=39.0), None) == []
    assert banderas_de_tendencia(estado(dolor=9, fiebre=39.0), {}) == []


def test_un_escalon_plano_no_dispara() -> None:
    """La forma que tiene el deterioro en el dataset oficial.

    `pac_42_00017` va dolor 4, 4 -> 9 y fiebre 37.4, 37.4 -> 37.9. En el dia del salto
    el paciente ya esta etiquetado rojo, asi que la tendencia no aporta nada; en los
    dias anteriores no hay delta ninguno que ver.
    """

    # El dia 3, comparado con el dia 1: valores identicos.
    assert banderas_de_tendencia(
        estado(dolor=4, fiebre=37.4), serie(dolor=[(1, 4)], fiebre=[(1, 37.4)])
    ) == []


def test_una_subida_por_debajo_del_umbral_no_dispara() -> None:
    """El punto de operacion se eligio midiendo: por debajo, solo falsas alarmas."""

    justo_debajo = SALTO_DOLOR_NRS - 1
    assert banderas_de_tendencia(
        estado(dolor=2 + justo_debajo), serie(dolor=[(3, 2)])
    ) == []

    assert banderas_de_tendencia(
        estado(fiebre=round(36.5 + SALTO_FIEBRE_C - 0.1, 2)), serie(fiebre=[(3, 36.5)])
    ) == []


def test_una_bajada_nunca_dispara() -> None:
    assert banderas_de_tendencia(estado(dolor=1), serie(dolor=[(3, 9)])) == []
    assert banderas_de_tendencia(estado(fiebre=36.2), serie(fiebre=[(3, 38.5)])) == []


def test_un_dominio_sin_valor_hoy_no_dispara() -> None:
    """No se compara contra lo que no se sabe."""

    assert banderas_de_tendencia(estado(), serie(dolor=[(3, 2)])) == []


def test_un_valor_anterior_no_numerico_se_ignora() -> None:
    """La tabla guarda los seis dominios en una columna: llegan cadenas como 'normal'."""

    assert banderas_de_tendencia(estado(dolor=9), {"dolor": [(3, "normal")]}) == []


# ---------------------------------------------------------------------------
# Integracion con el motor
# ---------------------------------------------------------------------------

def test_el_motor_sin_historia_decide_exactamente_igual() -> None:
    motor = TriageEngine()
    e = estado(dolor=8, fiebre=37.4)

    sin = motor.evaluar(e, cerrar=True)
    con_vacia = motor.evaluar(e, cerrar=True, historia={})

    assert sin.nivel == con_vacia.nivel
    assert [b.codigo for b in sin.banderas_amarillas] == [
        b.codigo for b in con_vacia.banderas_amarillas
    ]


def test_la_tendencia_llega_a_la_decision_como_bandera_amarilla() -> None:
    motor = TriageEngine()
    # Dolor 6: por encima del umbral amarillo (5) y por debajo del rojo (7). Con 8 el
    # caso ya era rojo por criterio absoluto y la tendencia no se veia.
    e = estado(dolor=6, fiebre=36.5)

    d = motor.evaluar(e, cerrar=True, historia=serie(dolor=[(3, 2)]))

    codigos = [b.codigo for b in d.banderas_amarillas]
    assert "T1_DOLOR_ASCENDENTE" in codigos
    # Con la bandera de dolor absoluto (A1) mas la de tendencia son dos, y dos banderas
    # simultaneas es el criterio de escalamiento del motor.
    assert d.nivel is Nivel.AMARILLO
    assert not d.provisional


def test_la_tendencia_no_puede_bajar_una_criticidad_roja() -> None:
    """Ninguna senal nueva puede retirar un criterio de alarma ya cumplido."""

    motor = TriageEngine()
    e = estado(dolor=9, fiebre=39.0)

    d = motor.evaluar(e, cerrar=True, historia=serie(dolor=[(3, 9)], fiebre=[(3, 39.0)]))

    assert d.nivel is Nivel.ROJO


@pytest.mark.parametrize("dia_anterior", [1, 3, 7])
def test_el_dia_de_la_llamada_anterior_aparece_en_la_bandera(dia_anterior: int) -> None:
    """Sin el dia, la bandera no se puede auditar contra la llamada de la que salio."""

    hits = banderas_de_tendencia(estado(dolor=9), serie(dolor=[(dia_anterior, 2)]))

    assert f"dia {dia_anterior}" in hits[0].valor_observado
