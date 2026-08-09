"""El cierre adaptativo no puede quedarse con un dato clínico a medias.

El mercado de agentes de voz publica la **tasa de cortes falsos** como el eje en el que se
comparan los detectores de fin de turno. Aquí el cierre lo decide `dialog/completitud.py`
—código con conocimiento del dominio, no un modelo— y la medición equivalente no necesita
audio nuevo: **una pausa a mitad de frase deja al servidor con un prefijo de la
respuesta**, así que basta recorrer los prefijos y preguntarle al cierre qué haría.

Hecho eso (`eval/cortes_falsos.py`), aparecieron dos, y los dos eran el mismo defecto:

    "Treinta y siete cinco"                  ->  cerraba en "Treinta y siete"  ->  37.0
    "Me la tomé y estaba en treinta y ocho dos" -> cerraba en "...treinta y ocho" -> 38.0

**37.5 cumple la bandera amarilla de febrícula (>= 37.4) y 37.0 no cumple nada.** El cierre
anticipado no perdía precisión: borraba la bandera. Un falso negativo clínico para ahorrar
450 ms es el peor cambio posible en este sistema, y la rúbrica lo pesa por encima de todo
lo demás.

La causa es del idioma: en español la décima se dice **después** del entero —«treinta y
ocho *dos*», «treinta y siete *cinco*», «treinta y ocho *y medio*»— así que un entero es un
prefijo tan válido como un valor final. Ante esa ambigüedad, el módulo ya declaraba la
regla correcta en su propia documentación: *la duda siempre se resuelve escuchando más,
nunca menos.*
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))

from centinela.dialog.completitud import (  # noqa: E402
    MS_CIERRE_MINIMO,
    respuesta_completa,
)


# ==========================================================================
# 1. Los dos casos que fallaban
# ==========================================================================

@pytest.mark.parametrize("prefijo", [
    "Treinta y siete",
    "Me la tomé y estaba en treinta y ocho",
    "treinta y ocho",
    "tengo treinta y nueve",
])
def test_una_temperatura_sin_decimal_no_cierra_el_turno(prefijo: str) -> None:
    """Puede estar creciendo: la décima se dice después."""

    v = respuesta_completa(prefijo, "fiebre", MS_CIERRE_MINIMO)
    assert not v.completa, f"cerro con «{prefijo}», que puede seguir con la decima"


@pytest.mark.parametrize("completa", [
    "Treinta y siete cinco",
    "Me la tomé y estaba en treinta y ocho dos",
    "treinta y ocho y medio",
])
def test_una_temperatura_con_decimal_si_cierra(completa: str) -> None:
    """Con la décima dicha no falta nada, y esperar el techo sería regalar 450 ms."""

    v = respuesta_completa(completa, "fiebre", MS_CIERRE_MINIMO)
    assert v.completa, f"no cerro con «{completa}», que ya trae la decima"


def test_negar_la_fiebre_sigue_cerrando() -> None:
    """La regla nueva es sobre el número, no sobre el dominio: quien niega no tiene décima
    que decir."""

    assert respuesta_completa("No he tenido fiebre", "fiebre", MS_CIERRE_MINIMO).completa
    assert respuesta_completa(
        "No me la he tomado", "fiebre", MS_CIERRE_MINIMO
    ).completa


def test_el_dolor_no_se_ve_afectado() -> None:
    """El NRS es una escala entera de 0 a 10: «un seis» no es un prefijo de nada."""

    assert respuesta_completa("como un seis", "dolor", MS_CIERRE_MINIMO).completa


def test_el_conector_colgante_sigue_mandando_sobre_todo() -> None:
    """La guarda que ya existía no se toca: «seis pero» no está completa aunque tenga el
    seis, porque lo que va después del «pero» suele ser el matiz clínico."""

    assert not respuesta_completa("como un seis pero", "dolor", MS_CIERRE_MINIMO).completa


# ==========================================================================
# 2. La medición existe, y dice cero
# ==========================================================================

@pytest.fixture
def informe() -> dict:
    ruta = RAIZ / "docs" / "metrics" / "cortes_falsos.json"
    if not ruta.exists():
        pytest.skip("falta `make cortes`")
    return json.loads(ruta.read_text(encoding="utf-8"))


def test_ningun_corte_anticipado_cambia_el_dato_clinico(informe: dict) -> None:
    assert informe["cortes_falsos_con_costo_clinico"] == 0


def test_el_cierre_adaptativo_sigue_sirviendo_para_algo(informe: dict) -> None:
    """El arreglo tiene un precio y hay que verlo: si nadie cerrara antes, el cierre
    adaptativo sería código muerto y los 900 ms del techo la única conducta."""

    assert informe["cierran_en_un_prefijo"] >= 3


def test_el_informe_separa_el_ahorro_gratis_del_corte_que_cuesta(informe: dict) -> None:
    """La tasa del mercado mezcla los dos. Cerrar antes cuando el paciente ya dijo el
    número y lo que faltaba era relleno es exactamente lo que se buscaba."""

    assert "de_esas_sin_costo_clinico" in informe
    assert (
        informe["de_esas_sin_costo_clinico"]
        + informe["cortes_falsos_con_costo_clinico"]
        == informe["cierran_en_un_prefijo"]
    )
