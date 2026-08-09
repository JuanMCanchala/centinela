"""La carpeta no es la etiqueta. Y el corpus del reto trae la trampa puesta.

En 2026 este modo de fallo tiene nombre: **deceptive grounding**. Una respuesta clínica
puede pasar todas las comprobaciones automáticas —cero alucinaciones, fidelidad al pasaje,
citas reales— y hablar de **la entidad equivocada**. Es invisible a las métricas de
fidelidad, porque cada afirmación viene de un documento real; lo que está mal es de quién
habla ese documento.

Aquí la entidad es el procedimiento, y `dataset/textos/breast_cancer/` contiene 19 PDFs que
son todos de cáncer de **cuello uterino**. Una ingesta que se crea el nombre de la carpeta
responde una pregunta de mastectomía citando `002-GUIA-DE-CANCER-DE-CUELLO-UTERINO.pdf`:
archivo real, carpeta correcta, enfermedad equivocada, y ninguna métrica de fidelidad se
queja.

La defensa es una sola línea de diseño —**el tema se detecta por contenido, no por ruta**—
y estas pruebas la fijan, porque es de las que se rompen sin ruido: bastaría con pasar
`tema_declarado` al ingerir para que la carpeta volviera a mandar.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))

from centinela.rag.ingest import clasificar_tema  # noqa: E402


# ==========================================================================
# 1. El clasificador lee el contenido
# ==========================================================================

def test_un_texto_de_cuello_uterino_no_se_clasifica_como_mama() -> None:
    """El caso exacto del dataset: contenido cervical, carpeta de mama."""

    texto = (
        "Guia de practica clinica para el cancer de cuello uterino. La citologia "
        "cervicouterina y la prueba de virus del papiloma humano son las herramientas de "
        "tamizacion. La conizacion y la histerectomia radical con linfadenectomia pelvica "
        "son opciones segun el estadio del cuello uterino. El seguimiento del cuello "
        "uterino tras el tratamiento incluye colposcopia."
    )

    tema, _puntajes = clasificar_tema(texto)

    assert tema != "cancer_mama", "el contenido cervical se etiqueto como mama"
    assert tema == "cancer_cuello_uterino"


def test_el_clasificador_devuelve_los_puntajes_para_poder_auditarlo() -> None:
    """Una etiqueta sin sus puntajes no se puede discutir, y esta hay que poder discutirla."""

    _tema, puntajes = clasificar_tema("apendicectomia laparoscopica por apendicitis aguda")

    assert puntajes
    assert "cancer_mama" in puntajes


# ==========================================================================
# 2. La ingesta no le pasa la carpeta como tema
# ==========================================================================

def test_la_subida_por_consola_no_declara_tema() -> None:
    """`extraer_documento(ruta, tema_declarado, ...)` acepta un tema declarado, y ahí es
    donde la carpeta podría colarse. El servidor pasa `None` a propósito."""

    fuente = (RAIZ / "api" / "centinela" / "main.py").read_text(encoding="utf-8")
    assert "extraer_documento, destino, None, True" in fuente, (
        "el endpoint de subida empezo a declarar un tema en vez de detectarlo"
    )


def test_el_documento_registra_el_tema_detectado_y_no_el_declarado() -> None:
    fuente = (RAIZ / "api" / "centinela" / "main.py").read_text(encoding="utf-8")
    assert "tema=doc.tema_detectado" in fuente


# ==========================================================================
# 3. La medición existe y dice cero
# ==========================================================================

@pytest.fixture
def informe() -> dict:
    ruta = RAIZ / "docs" / "metrics" / "atribucion.json"
    if not ruta.exists():
        pytest.skip("falta `make atribucion`")
    return json.loads(ruta.read_text(encoding="utf-8"))


def test_ninguna_trampa_se_apoyo_en_otro_procedimiento(informe: dict) -> None:
    assert informe["citas_de_otro_procedimiento"] == 0


def test_la_trampa_del_dataset_esta_entre_las_medidas(informe: dict) -> None:
    """Que el arnés incluya el caso que el material entregado hace posible."""

    del_dataset = [
        t for t in informe["trampas"]
        if t["procedimiento"] == "Mastectomía"
        and t["tema_ajeno_de_la_pregunta"] == "cancer_cuello_uterino"
    ]
    assert del_dataset, "no se mide la trampa que el propio dataset planta"
    for t in del_dataset:
        assert not t["citas_ajenas"], (
            "una pregunta de mastectomia se respondio con material de cuello uterino"
        )


def test_el_informe_publica_las_dos_caras_y_no_solo_la_buena(informe: dict) -> None:
    """«Respondió 8 de 10» leído solo se toma por un fallo. La cifra que lo explica —en
    cuántas el modelo dijo que no tenía el dato y redirigió— tiene que ir al lado."""

    assert "de_esas_declino_el_dato" in informe
    assert "matiz" in informe
