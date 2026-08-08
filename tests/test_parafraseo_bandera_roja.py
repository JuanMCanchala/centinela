"""Que una bandera roja se detecte aunque el paciente no use la palabra del léxico.

El léxico cualitativo sale del vocabulario observado en los 3.991 turnos del dataset
oficial, y acepta las categorías de hallazgo sin exigir contexto de dominio. Lo que no
estaba medido era su **cobertura**: qué pasa cuando el paciente describe lo mismo con
otras palabras.

La familia `parafraseo_rojo` de `eval/redteam.py` lo midió y encontró un hueco real:

    "No me puedo sostener, tengo que arrastrarme para llegar al baño"

cerraba en amarillo con el dominio de movilidad sin resolver. Es incapacidad funcional
por cualquier lectura clínica, y era el falso negativo que no se ve — no hay error, no
hay excepción, solo una llamada que cierra en amarillo cuando debía cerrar en rojo.

La lección detrás del arreglo: **el paciente que no puede moverse rara vez lo dice con
un verbo de negación**. Lo dice contando lo que tiene que hacer para lograrlo —
arrastrarse, gatear, ir en cuatro patas — o describiendo la parte del cuerpo como si no
le perteneciera: «la pierna no me responde».

Estas pruebas fijan esa cobertura, y las dos primeras la protegen por los dos lados: lo
que ahora sí se detecta, y lo que no puede empezar a detectarse de más (una limitación
esperada no es una incapacidad).
"""

from __future__ import annotations

import pytest

from centinela.clinical.normalizer import pistas_cualitativas
from centinela.clinical.triage_engine import TriageEngine
from centinela.models import ClinicalState, Movilidad, Nivel, Observacion, Procedencia

# Formas de decir "no puedo moverme" sin usar ninguna de las que el léxico ya conocía.
INCAPACIDAD = [
    "No me puedo sostener, tengo que arrastrarme para llegar al bano",
    "me arrastro hasta el bano porque no me sostengo",
    "la pierna no me responde nada desde ayer",
    "no me responde la pierna",
    "llego al bano gateando",
    "tengo que andar en cuatro patas",
]


@pytest.mark.parametrize("dice", INCAPACIDAD)
def test_la_incapacidad_descrita_por_la_conducta_se_detecta(dice: str) -> None:
    pistas = pistas_cualitativas(dice, "movilidad")

    assert pistas.get("movilidad") == Movilidad.INCAPACITANTE_NUEVA.value, dice


# Lo que NO puede empezar a marcarse como incapacidad. Ampliar un léxico de alarma
# tiene un coste simétrico: sobre-detectar convierte llamadas normales en rojos.
LIMITACION_O_NORMAL = [
    ("camino despacio pero camino", "limitada_esperada"),
    ("me cuesta un poco levantarme de la cama", "limitada_esperada"),
    ("ando con el caminador", "limitada_esperada"),
    ("apoyandome en la pared llego bien", "limitada_esperada"),
    ("Camino normal, sin ningun problema", "normal"),
    ("me muevo bien", "normal"),
    ("subo escaleras sin dificultad", "normal"),
]


@pytest.mark.parametrize("dice, espera", LIMITACION_O_NORMAL)
def test_una_limitacion_esperada_no_se_vuelve_incapacidad(dice: str, espera: str) -> None:
    pistas = pistas_cualitativas(dice, "movilidad")

    assert pistas.get("movilidad") == espera, dice


def test_la_parafrasis_llega_hasta_la_decision_roja() -> None:
    """La cobertura del léxico solo sirve si el motor escala con ella.

    `R4_MOVILIDAD` se dispara con `incapacitante_nueva`, así que la prueba recorre el
    camino completo: palabras del paciente → categoría → regla roja.
    """

    pistas = pistas_cualitativas(
        "No me puedo sostener, tengo que arrastrarme para llegar al bano", "movilidad"
    )

    estado = ClinicalState()
    estado.movilidad = Observacion(
        valor=pistas["movilidad"],
        conocido=True,
        procedencia=Procedencia(turno_idx=4, cita_paciente="tengo que arrastrarme"),
    )

    decision = TriageEngine().evaluar(estado, cerrar=True)

    assert decision.nivel is Nivel.ROJO
    assert [r.codigo for r in decision.reglas_rojas] == ["R4_MOVILIDAD"]
