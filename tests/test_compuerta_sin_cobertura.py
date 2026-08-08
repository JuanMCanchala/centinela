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
        categoria = None  # del corpus oficial del reto

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


# ==========================================================================
# El liston del material complementario
#
# El corpus complementario se redujo a un solo folleto (23 fragmentos) porque los
# organizadores confirmaron que el desajuste de `dataset/textos/breast_cancer/` era
# INTENCIONAL, para evaluar el criterio analitico, y que el enfoque correcto es ajustar
# el modelo y no el corpus.
#
# Esa reduccion destapo un defecto de la compuerta: con `similitud OR solape`, la
# similitud daba luz verde siempre. Medido sobre ese folleto, las siete preguntas de
# prueba caen entre 0.843 y 0.883 de similitud -- todas por encima del 0.82 -- mientras
# el solape lexico separa limpio (0.00-0.25 lo que no cubre, 0.50-0.75 lo que si). El
# sintoma: a "que hago si me sale liquido de la herida" el agente contestaba "bombee con
# el puño 10 veces", que es un ejercicio de linfedema.
# ==========================================================================

def _pasaje(similitud: float, solape: float, categoria: str | None):
    class Pasaje:
        pass
    p = Pasaje()
    p.similitud = similitud
    p.solape_lexico = solape
    p.categoria = categoria
    return p


def test_el_complemento_solo_no_responde_si_el_solape_es_bajo() -> None:
    """El caso real y peligroso: similitud alta, solape 0.25, tema correcto."""

    r = _recuperador({"cancer_mama"})

    fundamentado, razon = r._evaluar_fundamentacion(
        [_pasaje(0.859, 0.25, "complementario")], cobertura=True,
        tema_esperado="cancer_mama", tema_filtrado="cancer_mama",
    )

    assert fundamentado is False
    assert "complementario" in razon


def test_el_complemento_si_responde_lo_que_de_verdad_cubre() -> None:
    """La contraparte: "que ejercicios puedo hacer con el brazo" da 0.883 / 0.75."""

    r = _recuperador({"cancer_mama"})

    fundamentado, _ = r._evaluar_fundamentacion(
        [_pasaje(0.883, 0.75, "complementario")], cobertura=True,
        tema_esperado="cancer_mama", tema_filtrado="cancer_mama",
    )

    assert fundamentado is True


def test_el_corpus_oficial_conserva_el_criterio_permisivo() -> None:
    """El liston nuevo es SOLO para el complemento.

    En el corpus oficial el `or` es correcto: cubre el tema entero, asi que una pregunta
    parafraseada puede tener poco solape lexico y estar bien respondida. Subirle el
    liston romperia las 48 preguntas que hoy se responden.
    """

    r = _recuperador({"apendicitis"})

    fundamentado, _ = r._evaluar_fundamentacion(
        [_pasaje(0.86, 0.05, "oficial")], cobertura=True,
        tema_esperado="apendicitis", tema_filtrado="apendicitis",
    )

    assert fundamentado is True


def test_un_pasaje_oficial_entre_complementarios_no_activa_el_liston() -> None:
    """El liston pide que TODO el soporte sea complementario."""

    r = _recuperador({"cancer_mama"})

    fundamentado, _ = r._evaluar_fundamentacion(
        [_pasaje(0.86, 0.05, "complementario"), _pasaje(0.86, 0.05, None)],
        cobertura=True, tema_esperado="cancer_mama", tema_filtrado="cancer_mama",
    )

    assert fundamentado is True
