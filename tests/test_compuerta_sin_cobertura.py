"""La compuerta que se niega a responder cuando el corpus no cubre el procedimiento.

Este archivo existe porque **tapar el hueco de mastectomia dejo a esta garantia sin forma
de demostrarse en vivo**, y eso es un efecto secundario que hay que decir, no dejar pasar.

Antes, `eval/humo.py` la demostraba de la forma mas convincente posible: sobre un paciente
real del dataset, con un procedimiento real, el agente se negaba a responder porque el
corpus entregado no traia ni un documento de cancer de mama. Al ingerir guia publica para
tapar ese hueco (`scripts/ingerir_complementario.py`), los cinco procedimientos quedaron
cubiertos y esa rama de la compuerta ya no se puede disparar con ningun paciente del
dataset.

La garantia sigue existiendo y sigue importando -- un despliegue real recibe procedimientos
que el corpus no cubre, y ahi la unica respuesta aceptable es "no lo se" -- asi que se prueba
aqui, contra la funcion, en vez de contra el sistema montado.

Lo que NO prueba este archivo: que la similitud sea un buen juez de si un pasaje responde la
pregunta. No lo es por si sola; para eso esta la verificacion posterior a la generacion
(`rag/answerer.py`), que `make rag` mide con 0 cifras sin respaldo sobre 60 preguntas.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from centinela.rag.retriever import Retriever  # noqa: E402


class StoreFalso:
    """Un indice del que solo importa cuantos documentos tiene de cada tema."""

    def __init__(self, temas_con_documentos) -> None:
        self.temas = set(temas_con_documentos)
        self.generacion = 1

    def estadisticas(self) -> dict:
        return {"por_tema": {t: 1 for t in self.temas}}


def _recuperador(temas) -> Retriever:
    r = Retriever.__new__(Retriever)
    r.store = StoreFalso(temas)
    return r


def test_un_tema_sin_documentos_no_tiene_cobertura() -> None:
    r = _recuperador({"apendicitis", "colecistitis"})

    assert r._hay_cobertura("cancer_mama") is False
    assert r._hay_cobertura("apendicitis") is True


def test_sin_procedimiento_conocido_no_se_afirma_falta_de_cobertura() -> None:
    """`None` es "no se de que procedimiento hablamos", no "no hay documentos"."""

    r = _recuperador({"apendicitis"})

    assert r._hay_cobertura(None) is True


def test_sin_cobertura_la_compuerta_se_niega_aunque_haya_pasajes() -> None:
    """Es el orden que importa: la falta de cobertura manda sobre la similitud.

    Si se evaluara primero la similitud, un pasaje de otro tema con buen parecido
    superficial fundamentaria la respuesta -- que es exactamente la alucinacion por
    recuperacion que este proyecto documenta del corpus entregado.
    """

    r = _recuperador({"cancer_cuello_uterino"})

    class Pasaje:
        similitud = 0.99
        solape_lexico = 0.9

    fundamentado, razon = r._evaluar_fundamentacion(
        [Pasaje()], cobertura=False, tema_esperado="cancer_mama", tema_filtrado=None
    )

    assert fundamentado is False
    assert "cancer_mama" in razon or "cobertura" in razon.lower()


def test_con_cobertura_y_buen_pasaje_si_fundamenta() -> None:
    r = _recuperador({"cancer_mama"})

    class Pasaje:
        similitud = 0.9
        solape_lexico = 0.4

    fundamentado, _ = r._evaluar_fundamentacion(
        [Pasaje()], cobertura=True, tema_esperado="cancer_mama",
        tema_filtrado="cancer_mama",
    )

    assert fundamentado is True


def test_sin_pasajes_no_se_fundamenta_nada() -> None:
    r = _recuperador({"cancer_mama"})

    fundamentado, razon = r._evaluar_fundamentacion(
        [], cobertura=True, tema_esperado="cancer_mama", tema_filtrado="cancer_mama"
    )

    assert fundamentado is False
    assert razon
