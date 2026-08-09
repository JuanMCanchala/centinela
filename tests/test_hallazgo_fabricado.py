"""El modelo no puede inventar una bandera roja, y esto salió de una llamada real.

Lo que pasó, tal cual quedó en el registro de la llamada `0e5ec7e7`:

    agente   : «¿ese dolor le cede con las pastillas que le formularon, o sigue igual?»
    paciente : «cuando me tomo las pastillas ya no me hace el dolor»
    el STT   : «Cuando me tomo las pasillas ya me hace el dolor»     ← se comió el «no»
    el modelo: herida = secrecion_purulenta
    el motor : ROJO, ticket TK-0e5ec7e7-R

El motor de decisión no tiene culpa: decidió bien sobre un dato falso. Lo inventó la
percepción. Y el agente le leyó de vuelta al paciente *«me dice líquido amarillo o pus
saliendo de la herida, ¿es correcto?»*; lo negó dos veces y la alerta se quedó, porque un
desmentido no retira un ticket — regla correcta cuando el hallazgo es real.

`_aceptar_del_modelo` protegía **una sola dirección**: que el modelo no degradara lo que las
reglas ya habían detectado. Con el dominio vacío aceptaba cualquier valor, incluido el más
grave de la escala, salido de la nada. Vigilar solo el piso es no mirar la mitad del eje.

La condición que faltaba costó tres intentos, y los dos primeros están documentados en
`extractor._aceptar_del_modelo` porque fallaron de formas instructivas: exigir que el léxico
resolviera el dominio rompió cuatro paráfrasis rojas reales, y pedirle al modelo que citara
las palabras del paciente reveló que un 3.8B no puede copiar. La que aguanta le pregunta al
**texto**: ¿mencionó el paciente ese dominio, aunque fuera de pasada?
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))

from centinela.clinical.extractor import Extractor, GRAVEDAD  # noqa: E402
from centinela.clinical.normalizer import menciona, normalizar_turno  # noqa: E402
from centinela.models import ClinicalState  # noqa: E402


# ==========================================================================
# 1. La mención: la señal que sí distingue los casos
# ==========================================================================

@pytest.mark.parametrize("frase", [
    "Cuando me tomo las pasillas ya me hace el dolor",
    "Pues es que las pastillas esas me las tomo cada ocho horas y ya",
    "Mi hija me lleva el jueves a que me revisen en el centro de salud",
    "Yo lo pondria en un cinco",
])
def test_una_frase_que_no_habla_de_la_herida_no_la_menciona(frase: str) -> None:
    assert not menciona(frase, "herida")


@pytest.mark.parametrize("frase", [
    "Por donde me cortaron esta botando una cosa espesa color crema",
    "De la cortada sale un liquido gruesito, entre amarillo y verde",
    "Se me esta saliendo materia por la herida",
    "Le tuve que cambiar la gasa tres veces porque se empapa sola",
    "Los puntos se ven bien",
    "La cicatriz esta cerrando",
])
def test_las_parafrasis_reales_si_mencionan_la_herida(frase: str) -> None:
    """Son las cuatro que rompió el primer intento de arreglo, más dos benignas.

    El léxico de gravedad no las resuelve todas —«cambiar la gasa» no es una categoría de
    hallazgo— y por eso la mención es una pregunta distinta y una tabla distinta.
    """

    assert menciona(frase, "herida")


@pytest.mark.parametrize("frase", [
    "Desde ayer no logro pararme, la pierna no me responde",
    "No me puedo sostener, tengo que arrastrarme para llegar al bano",
    "Camino normal",
])
def test_la_movilidad_tambien_se_detecta(frase: str) -> None:
    assert menciona(frase, "movilidad")


def test_sin_tildes_da_igual() -> None:
    """El paciente dice «cicatriz» y el STT escribe «cicatríz» según el día."""

    assert menciona("la cicatríz está bien", "herida")


def test_un_dominio_numerico_no_se_puede_negar() -> None:
    """Dolor y fiebre no tienen lista: su respaldo lo da la regex que leyó la cifra."""

    assert menciona("cualquier cosa", "dolor")
    assert menciona("cualquier cosa", "fiebre")


# ==========================================================================
# 2. La guarda, sin invocar al modelo
# ==========================================================================

def _veredicto(texto: str, dominio: str, valor: str, dominio_objetivo: str):
    ex = Extractor(llm=None)  # type: ignore[arg-type]
    norm = normalizar_turno(texto, dominio_objetivo=dominio_objetivo)
    return ex._aceptar_del_modelo(
        ClinicalState(), dominio, valor, norm, dominio_objetivo
    )


def test_el_hallazgo_mas_grave_sin_mencion_ni_pregunta_se_rechaza() -> None:
    aceptado, motivo = _veredicto(
        "Cuando me tomo las pasillas ya me hace el dolor",
        "herida", "secrecion_purulenta", "dolor",
    )
    assert not aceptado
    assert "no se admite" in motivo.lower()


def test_si_el_paciente_lo_menciona_se_acepta_aunque_nadie_lo_preguntara() -> None:
    """Un paciente que se adelanta con una bandera roja tiene que entrar igual."""

    aceptado, _motivo = _veredicto(
        "Le tuve que cambiar la gasa tres veces porque se empapa sola",
        "herida", "secrecion_purulenta", "dolor",
    )
    assert aceptado


def test_si_se_estaba_preguntando_por_ese_dominio_se_acepta() -> None:
    """Sobre el dominio abierto el modelo es la tercera capa y su aporte es el objetivo."""

    aceptado, _motivo = _veredicto(
        "Pues no sabria decirle, raro", "herida", "secrecion_purulenta", "herida",
    )
    assert aceptado


def test_un_valor_intermedio_no_pasa_por_la_guarda() -> None:
    """La guarda es para el valor que dispara la alarma, no para toda aportación."""

    aceptado, _motivo = _veredicto(
        "Cuando me tomo las pasillas ya me hace el dolor",
        "herida", "eritema_leve", "dolor",
    )
    assert aceptado


@pytest.mark.parametrize("dominio", sorted(GRAVEDAD))
def test_la_guarda_cubre_los_cuatro_dominios_con_escala(dominio: str) -> None:
    """Inventar «muy_disminuido» de la nada también es inventar."""

    aceptado, _motivo = _veredicto(
        "Mi hija me lleva el jueves al centro de salud",
        dominio, GRAVEDAD[dominio][-1], "dolor",
    )
    assert not aceptado, f"{dominio} admite el valor mas grave sin mencion ni pregunta"


# ==========================================================================
# 3. Lo que la guarda NO puede romper
# ==========================================================================

def test_el_modelo_sigue_sin_poder_degradar_un_hallazgo_de_regla() -> None:
    """La protección original sigue en pie: es la mitad que ya existía del eje."""

    ex = Extractor(llm=None)  # type: ignore[arg-type]
    estado = ClinicalState()
    norm = normalizar_turno("sale pus de la herida", dominio_objetivo="herida")
    estado.herida.conocido = True
    estado.herida.valor = "secrecion_purulenta"

    aceptado, motivo = ex._aceptar_del_modelo(
        estado, "herida", "normal", norm, "herida"
    )
    assert not aceptado
    assert "no se degrada" in motivo


def test_la_suite_adversarial_vigila_las_dos_direcciones() -> None:
    """Guarda estructural: `Caso` tenía piso y no techo, que es el mismo sesgo que el bug."""

    fuente = (RAIZ / "eval" / "redteam.py").read_text(encoding="utf-8")
    assert "nivel_maximo" in fuente
    assert "la bandera se INVENTO" in fuente
    assert "hallazgo_fabricado" in fuente
