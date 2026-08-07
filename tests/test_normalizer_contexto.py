"""Un "normal" sin contexto no puede marcar dominios como normales.

Regresion de un fallo de seguridad clinica, encontrado mirando la interfaz: los
cuatro dominios cualitativos aparecian como "normal" citando el mismo turno, que
solo hablaba de dolor.

La causa: las listas de lexico incluian "normal" como termino suelto, asi que un
paciente preguntando *"el dolor es un 6, no se si eso es normal"* marcaba herida,
movilidad, apetito y sueno como normales de golpe.

Por que importa mas de lo que parece: el agente cree que ya conoce esos cuatro
dominios y no vuelve a preguntar por ellos. Un dominio marcado normal sin que
nadie lo haya dicho es un falso negativo esperando ocurrir -- exactamente lo que
el motor de decision existe para evitar, colandose una capa antes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from centinela.clinical.normalizer import (  # noqa: E402
    normalizar_turno,
    pistas_cualitativas,
)

# Turnos donde aparece la palabra "normal" pero el paciente NO esta reportando
# que algun dominio este normal: esta preguntando, o dudando.
PREGUNTA_SI_ES_NORMAL = (
    "Pues el dolor si esta fuerte, como un 6, y no se si es normal a estos dias",
    "El dolor es un 5, usted cree que eso sea normal?",
    "No se si sera normal lo que siento",
    "Tengo 37.8 de temperatura, eso es normal?",
    "Me duele bastante. Sera normal o me tengo que preocupar?",
)


@pytest.mark.parametrize("texto", PREGUNTA_SI_ES_NORMAL)
def test_preguntar_si_es_normal_no_marca_dominios(texto: str) -> None:
    pistas = pistas_cualitativas(texto)
    assert pistas == {}, (
        f"un turno que PREGUNTA si algo es normal marco dominios como normales: "
        f"{pistas} en {texto!r}"
    )


@pytest.mark.parametrize("texto", PREGUNTA_SI_ES_NORMAL)
def test_ni_siquiera_con_dominio_ajeno_preguntado(texto: str) -> None:
    """Preguntar por el dolor no autoriza a marcar la herida como normal."""

    pistas = pistas_cualitativas(texto, dominio_objetivo="dolor")
    assert "herida" not in pistas
    assert "apetito" not in pistas
    assert "sueno" not in pistas


# Reportes legitimos: el paciente si esta diciendo que un dominio esta bien.
REPORTES_NORMALES = (
    ("La herida se ve bien, limpiecita", "herida", "normal"),
    ("La herida esta normal, no le veo nada raro", "herida", "normal"),
    ("Camino normal, sin problema", "movilidad", "normal"),
    ("He comido normal, como siempre", "apetito", "normal"),
    ("Duermo bien, de corrido toda la noche", "sueno", "normal"),
)


@pytest.mark.parametrize("texto,dominio,esperado", REPORTES_NORMALES)
def test_reporte_legitimo_si_se_registra(texto: str, dominio: str, esperado: str) -> None:
    pistas = pistas_cualitativas(texto)
    assert pistas.get(dominio) == esperado, (
        f"no se registro un reporte legitimo de {dominio}: {pistas} en {texto!r}"
    )


def test_normal_ambiguo_se_acepta_si_es_el_dominio_preguntado() -> None:
    """Si el agente pregunto por la herida y el paciente dice "se ve bien", es la herida.

    Sin el dominio preguntado, un "bien" suelto no se atribuye a nada; con el, si.
    """

    sin_contexto = pistas_cualitativas("Se ve bien")
    con_contexto = pistas_cualitativas("Se ve bien", dominio_objetivo="herida")

    assert "herida" not in sin_contexto
    assert con_contexto.get("herida") == "normal"


# Los hallazgos si se aceptan siempre: son especificos y el paciente solo puede
# haberlos dicho a proposito.
HALLAZGOS = (
    ("Me sale un liquido amarillo de ahi", "herida", "secrecion_purulenta"),
    ("La veo como rojita alrededor", "herida", "eritema_leve"),
    ("De un dia para otro no puedo apoyar el pie", "movilidad", "incapacitante_nueva"),
    ("No me da hambre, casi no como", "apetito", "muy_disminuido"),
    ("No he podido dormir, me despierto a cada rato", "sueno", "muy_alterado"),
)


@pytest.mark.parametrize("texto,dominio,esperado", HALLAZGOS)
def test_hallazgos_se_aceptan_sin_necesitar_contexto(
    texto: str, dominio: str, esperado: str
) -> None:
    pistas = pistas_cualitativas(texto)
    assert pistas.get(dominio) == esperado, (
        f"se perdio un hallazgo clinico: {pistas} en {texto!r}"
    )


def test_el_turno_del_dataset_que_expuso_el_fallo() -> None:
    """El turno textual del caso rojo, que marcaba cuatro dominios de golpe."""

    turno = "Pues el dolor si esta fuerte, como un 6, y no se si es normal a estos dias"
    n = normalizar_turno(turno, dominio_objetivo="dolor")

    assert n.numeros.dolor_nrs == 6, "el dolor si debe extraerse"
    assert n.pistas == {}, f"no debe marcar ningun dominio cualitativo: {n.pistas}"


def test_gravedad_gana_sobre_normalidad() -> None:
    """Si el turno dice las dos cosas, gana el hallazgo grave."""

    pistas = pistas_cualitativas(
        "La herida se ve bien por fuera pero le sale un liquido amarillo"
    )
    assert pistas.get("herida") == "secrecion_purulenta"
