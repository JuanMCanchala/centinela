"""Una pista corta no puede engancharse dentro de otra palabra.

El léxico de pistas clínicas casa por **subcadena**, y eso es deliberado: `"enrojecid"` tiene
que coger «enrojecida» y «enrojecido», `"purulent"` las dos formas, `"supura"` coge
«supurando». El precio es que una pista que además es una palabra corta puede aparecer dentro
de otra que significa algo distinto —o lo contrario— y ahí el fallo no es cosmético: cambia el
dominio o el nivel de un hallazgo clínico sin que nada se queje.

Los dos casos de aquí salieron de llamadas reales, no de leer el código:

    "me esta saliendo un liquido amarillento"   ->  movilidad: limitada_esperada
        `"lento"` vive dentro de "amari**llento**". Un hallazgo ROJO de herida archivado
        como uno amarillo, y de otro dominio. En la llamada se vio como un bucle: el agente
        contestaba «corrijo entonces, me queda que se mueve con algo de dificultad», nunca
        registraba la herida, y repreguntaba lo mismo turno tras turno porque el dominio
        seguía sin resolverse.

    "la herida esta anormal"                    ->  herida: normal
        `"normal"` vive dentro de "**a**normal", que significa lo contrario. Es el falso
        negativo más literal que puede tener este sistema: el paciente describe el hallazgo
        como anormal y queda archivado como normal.

La corrección no es una regla general de frontera de palabra, porque eso rompería los
prefijos deliberados. Son dos listas explícitas y auditables, y con lados distintos: `"lento"`
la necesita por los dos, y `"normal"` **solo por la izquierda**, porque «normalidad» y
«normalmente» sí quieren decir normal y son como habla la gente.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))

from centinela.clinical.normalizer import normalizar_turno  # noqa: E402


def pistas(texto: str) -> dict:
    return normalizar_turno(texto).pistas


# ----------------------------------------------------------------------
# "lento" dentro de "amarillento"


@pytest.mark.parametrize("dicho", [
    "Me está saliendo un líquido amarillento.",
    "Me sale un líquido amarillento.",
    "veo un liquido amarillento en la herida",
    "la herida tiene una secreción amarillenta",
])
def test_liquido_amarillento_es_secrecion_purulenta(dicho):
    """Y sobre todo: NO es un hallazgo de movilidad."""

    p = pistas(dicho)

    assert p.get("herida") == "secrecion_purulenta"
    assert "movilidad" not in p


@pytest.mark.parametrize("dicho", [
    "camino muy lento",
    "me muevo lento",
    "ando lento por la casa",
])
def test_lento_de_verdad_sigue_siendo_movilidad(dicho):
    """La frontera de palabra no puede costar la pista que sí queríamos."""

    assert pistas(dicho).get("movilidad") == "limitada_esperada"


# ----------------------------------------------------------------------
# "normal" dentro de "anormal"


@pytest.mark.parametrize("dicho", [
    "la herida esta anormal",
    "veo la herida anormal",
    "la siento anormal",
])
def test_anormal_no_se_archiva_como_normal(dicho):
    """Sin pista es correcto: se pregunta otra vez o decide el modelo.

    Lo que no puede pasar es archivarlo como normal, que es lo contrario de lo que dijo el
    paciente y cierra el dominio con el hallazgo tranquilizador.
    """

    assert pistas(dicho).get("herida") != "normal"


@pytest.mark.parametrize("dicho,dominio", [
    ("la herida esta normal", "herida"),
    ("duermo con normalidad", "sueno"),
    ("como normalmente", "apetito"),
])
def test_las_formas_productivas_de_normal_siguen_valiendo(dicho, dominio):
    """La frontera es de un solo lado justo para no perder estas."""

    assert pistas(dicho).get(dominio) == "normal"


# ----------------------------------------------------------------------
# Los prefijos deliberados no se tocan


@pytest.mark.parametrize("dicho,dominio,categoria", [
    ("La herida está enrojecida", "herida", "eritema_leve"),
    ("la veo enrojecido alrededor", "herida", "eritema_leve"),
    ("está supurando", "herida", "secrecion_purulenta"),
    ("la herida está purulenta", "herida", "secrecion_purulenta"),
])
def test_los_prefijos_siguen_casando_por_subcadena(dicho, dominio, categoria):
    """`enrojecid`, `supura` y `purulent` son prefijos a propósito, no palabras."""

    assert pistas(dicho).get(dominio) == categoria


def test_ninguna_pista_corta_se_esconde_en_el_vocabulario_del_dominio():
    """Barrido sobre las palabras del dominio donde una pista podría colarse.

    Es la prueba que habría cazado los dos defectos antes de una llamada real. Cubre el
    vocabulario clínico que el paciente usa de verdad, no un diccionario entero.
    """

    from centinela.clinical import normalizer as N

    vocabulario = (
        "amarillento amarillenta violento lentamente talento anormal "
        "somnolencia molestia malestar cicatrizando alimento"
    ).split()

    lexicos = {
        "herida": N.PISTAS_HERIDA,
        "movilidad": N.PISTAS_MOVILIDAD,
        "apetito": N.PISTAS_APETITO,
        "sueno": N.PISTAS_SUENO,
    }

    colisiones = []
    for dominio, mapa in lexicos.items():
        for categoria, terminos in mapa.items():
            for t in terminos:
                # Solo las pistas de una palabra: las de varias no se esconden dentro de una.
                escondida = (
                    " " not in t
                    and t not in N.PISTAS_CON_FRONTERA
                    and t not in N.PISTAS_CON_FRONTERA_IZQUIERDA
                    and any(t in palabra and t != palabra for palabra in vocabulario)
                )
                if escondida:
                    dentro = [p for p in vocabulario if t in p and t != p]
                    # Un prefijo cuya palabra completa empieza por la pista es deliberado:
                    # "hinchad" dentro de "hinchado" es exactamente lo que se busca.
                    if any(not p.startswith(t) for p in dentro):
                        colisiones.append(f"{dominio}/{categoria}: {t!r} dentro de {dentro}")

    assert colisiones == [], "pistas que se esconden dentro de otra palabra: " + "; ".join(
        colisiones
    )
