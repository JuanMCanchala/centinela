"""El embedder: los prefijos asimetricos de E5 y la cache del modelo.

`rag/embedder.py` era el ultimo modulo del sistema sin prueba unitaria. Estaba cubierto
de facto por `make rag` -- 60 preguntas contra el corpus real -- y eso es cobertura de
integracion: mide que la recuperacion funciona, no que lo haga por la razon correcta.

Lo que hay que fijar aqui es una propiedad que FALLA EN SILENCIO, y es la unica de todo
el modulo con esa forma:

    La familia E5 esta entrenada con prefijos asimetricos. Una consulta se embebe como
    "query: ..." y un pasaje como "passage: ...". Confundirlos no lanza, no avisa, y no
    rompe ninguna prueba de integracion de golpe: degrada la recuperacion unos puntos.

Un `make rag` que baje de 48/60 a 44/60 se lee como "el corpus es dificil", no como "el
prefijo esta mal". Por eso la prueba mira los prefijos exactos con los que se llama al
modelo, y no la calidad del resultado.

Nada de esto descarga el modelo: el modelo real son 1.3 GB y su unica responsabilidad
aqui seria convertir texto en vectores. Se sustituye por un doble que APUNTA lo que se
le pidio embeber, que es justo lo que se quiere observar.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from centinela.rag.embedder import (
    DIM_POR_DEFECTO,
    MODELO_POR_DEFECTO,
    Embedder,
    embedder_compartido,
)


class ModeloEspia:
    """Doble del `TextEmbedding` de fastembed.

    Devuelve vectores deterministas -- no se mira su contenido -- y guarda los textos
    exactos que recibio, con prefijo incluido.
    """

    def __init__(self, dim: int = 4) -> None:
        self.dim = dim
        self.recibidos: list[list[str]] = []

    def embed(self, textos):
        lote = list(textos)
        self.recibidos.append(lote)
        return iter([np.arange(self.dim, dtype=np.float32) for _ in lote])

    @property
    def ultimo_lote(self) -> list[str]:
        return self.recibidos[-1]


@pytest.fixture()
def espiado() -> tuple[Embedder, ModeloEspia]:
    """Un embedder con el modelo ya puesto: la propiedad `modelo` no importa nada."""

    emb = Embedder()
    espia = ModeloEspia()
    emb._modelo = espia
    return emb, espia


# ==========================================================================
# Los prefijos, que es todo el asunto
# ==========================================================================

def test_una_consulta_va_prefijada_como_consulta(espiado) -> None:
    emb, espia = espiado

    emb.embed_consulta("cuando hay que ir a urgencias")

    assert espia.ultimo_lote == ["query: cuando hay que ir a urgencias"]


def test_un_pasaje_va_prefijado_como_pasaje(espiado) -> None:
    emb, espia = espiado

    emb.embed_pasajes(["la fiebre por encima de 38 grados requiere valoracion"])

    assert espia.ultimo_lote == [
        "passage: la fiebre por encima de 38 grados requiere valoracion"
    ]


def test_los_dos_prefijos_son_distintos_para_el_MISMO_texto(espiado) -> None:
    """El fallo silencioso, dicho de la forma en que se manifestaria.

    Si alguien unificara los dos metodos -- que es la simplificacion tentadora, porque
    los dos «solo embeben texto» -- este es el que lo caza.
    """

    emb, espia = espiado
    texto = "secrecion purulenta en la herida"

    emb.embed_consulta(texto)
    emb.embed_pasajes([texto])

    como_consulta, como_pasaje = espia.recibidos[0][0], espia.recibidos[1][0]
    assert como_consulta != como_pasaje
    assert como_consulta.startswith("query: ")
    assert como_pasaje.startswith("passage: ")


def test_nadie_mas_escribe_los_prefijos_a_mano() -> None:
    """La razon por la que la propiedad se sostiene: un solo sitio los escribe.

    Si otro modulo empieza a componer `"query: "` por su cuenta, la asimetria deja de
    tener un dueno y vuelve a ser posible desincronizarla.
    """

    raiz = Path(__file__).resolve().parents[1] / "api" / "centinela"
    culpables = []
    for archivo in raiz.rglob("*.py"):
        if archivo.name != "embedder.py":
            texto = archivo.read_text(encoding="utf-8")
            # Las dos comillas: un `'query: '` con simples es igual de peligroso.
            escrito = any(
                f"{comilla}{prefijo}: " in texto
                for comilla in ('"', "'")
                for prefijo in ("query", "passage")
            )
            if escrito:
                culpables.append(archivo.name)

    assert not culpables, f"prefijos de E5 escritos fuera de embedder.py: {culpables}"


def test_un_lote_de_pasajes_devuelve_un_vector_por_pasaje(espiado) -> None:
    emb, espia = espiado

    vectores = emb.embed_pasajes(["uno", "dos", "tres"])

    assert len(vectores) == 3
    assert espia.ultimo_lote == ["passage: uno", "passage: dos", "passage: tres"]
    # Listas de Python, no arrays de numpy: lo que sigue es serializar a JSON para el
    # indice, y un `ndarray` no es serializable.
    assert all(isinstance(v, list) for v in vectores)


def test_un_lote_vacio_no_inventa_pasajes(espiado) -> None:
    """Un documento sin texto extraible -- un PDF de puras imagenes sin OCR -- llega
    aqui como lista vacia. Tiene que salir vacia, no con un `"passage: "` suelto."""

    emb, espia = espiado

    vectores = emb.embed_pasajes([])

    assert vectores == []
    assert espia.ultimo_lote == []


# ==========================================================================
# Dimension
#
# El indice se crea con una dimension fija. Equivocarla no degrada: revienta al
# insertar, o peor, deja un indice de dimension incorrecta que hay que reconstruir.
# ==========================================================================

def test_la_dimension_de_e5_large_es_1024() -> None:
    assert Embedder(nombre_modelo=MODELO_POR_DEFECTO).dim == DIM_POR_DEFECTO
    assert Embedder(nombre_modelo=MODELO_POR_DEFECTO).dim == 1024


def test_otro_modelo_declara_768() -> None:
    assert Embedder(nombre_modelo="intfloat/multilingual-e5-base").dim == 768


# ==========================================================================
# La cache del modelo en disco
#
# Esto no es cosmetico: por defecto fastembed cachea en el temporal del sistema, que
# Windows limpia sin avisar. La consecuencia es una re-descarga de 1.3 GB en el peor
# momento posible, que es el arranque cronometrado de la compuerta G2.
# ==========================================================================

def test_la_cache_del_modelo_esta_dentro_del_proyecto() -> None:
    ruta = Path(os.environ["FASTEMBED_CACHE_PATH"])

    assert ruta.name == "modelos"
    assert ruta.parent.name == "data"
    # Y no en el temporal del sistema, que es de donde se la quiso sacar.
    assert "temp" not in str(ruta).lower()


# ==========================================================================
# Configuracion y singleton
# ==========================================================================

def test_el_modelo_se_puede_fijar_por_entorno(monkeypatch) -> None:
    monkeypatch.setenv("CENTINELA_EMBED_MODEL", "otro/modelo-de-prueba")

    assert Embedder().nombre_modelo == "otro/modelo-de-prueba"


def test_el_argumento_manda_sobre_el_entorno(monkeypatch) -> None:
    monkeypatch.setenv("CENTINELA_EMBED_MODEL", "el/del-entorno")

    assert Embedder(nombre_modelo="el/del-argumento").nombre_modelo == "el/del-argumento"


def test_cero_hilos_significa_dejarlo_a_onnx(monkeypatch) -> None:
    """`CENTINELA_EMBED_THREADS=0` no es «cero hilos»: es «no pasar el parametro».

    Pasar `threads=0` a ONNX Runtime no lo deja decidir, y la diferencia se paga en el
    arranque cronometrado.
    """

    monkeypatch.setenv("CENTINELA_EMBED_THREADS", "0")

    assert Embedder().hilos is None


def test_los_hilos_se_pueden_fijar_por_entorno(monkeypatch) -> None:
    monkeypatch.setenv("CENTINELA_EMBED_THREADS", "4")

    assert Embedder().hilos == 4


def test_el_embedder_compartido_es_uno_solo() -> None:
    """Dos instancias serian dos copias del modelo en memoria: ~1.3 GB de mas."""

    assert embedder_compartido() is embedder_compartido()


def test_calentar_pasa_por_el_camino_de_consulta(espiado) -> None:
    """El calentamiento tiene que ejercitar el camino que se va a cronometrar."""

    emb, espia = espiado

    emb.calentar()

    assert espia.ultimo_lote[0].startswith("query: ")


def test_el_modelo_no_se_construye_hasta_que_se_usa() -> None:
    """Carga diferida: importar el modulo no puede costar 1.3 GB ni una descarga.

    Es lo que permite que `make test` corra sin el modelo, y que el arranque decida
    cuando pagarlo -- ver `calentar_al_arrancar` en config.
    """

    emb = Embedder()

    assert emb._modelo is None
