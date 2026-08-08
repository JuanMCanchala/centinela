"""El corredor de suites de la consola de pruebas.

Lo que importa de este modulo no es que sepa lanzar procesos, sino que **el
veredicto sea el codigo de salida del comando y nada mas**. Si algun dia alguien
intenta "mejorarlo" interpretando la salida de texto -- buscar la palabra
"passed", contar fallos, adivinar -- estos tests lo impiden: hay un comando que
imprime un exito rotundo y termina en 1, y tiene que reportarse como fallo.

Se prueba con comandos triviales (`python -c ...`) en vez de las suites de
verdad: correr las de verdad aqui tardaria minutos y necesitaria el servidor
levantado. Lo que se ejerce es el corredor, que es el codigo nuevo.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from centinela.pruebas import SUITES, CorredorPruebas, Suite  # noqa: E402


def suite(nombre: str, codigo: str, necesita_url: bool = False) -> Suite:
    return Suite(
        id=nombre,
        titulo=f"Suite {nombre}",
        que="de mentira, para probar el corredor",
        argumentos=["-c", codigo],
        necesita_url=necesita_url,
    )


async def esperar(corredor: CorredorPruebas, suite_id: str) -> dict:
    """Deja que la tarea del subproceso termine y devuelve su estado final."""

    tarea = corredor._ejecuciones[suite_id].tarea
    assert tarea is not None, "lanzar() deberia haber creado una tarea"
    await tarea
    return corredor.estado(suite_id)


# ==========================================================================
# El veredicto es el codigo de salida
# ==========================================================================

@pytest.mark.asyncio
async def test_codigo_cero_es_pasa() -> None:
    corredor = CorredorPruebas((suite("ok", "print('todo bien')"),))
    corredor.lanzar("ok", "http://127.0.0.1:8000")
    estado = await esperar(corredor, "ok")

    assert estado["estado"] == "ok"
    assert estado["codigo_salida"] == 0
    assert "todo bien" in estado["salida"]


@pytest.mark.asyncio
async def test_salida_optimista_con_codigo_uno_es_fallo() -> None:
    """El caso que protege la regla: texto de exito, codigo de fallo.

    Un comando puede imprimir "OK, 160/160 casos correctos" y aun asi terminar
    en 1. Quien manda es el codigo de salida.
    """

    codigo = "print('OK: 160/160 casos correctos, 0 fallos'); raise SystemExit(1)"
    corredor = CorredorPruebas((suite("mentiroso", codigo),))
    corredor.lanzar("mentiroso", "http://127.0.0.1:8000")
    estado = await esperar(corredor, "mentiroso")

    assert estado["estado"] == "fallo", "una salida optimista no puede tapar un codigo 1"
    assert estado["codigo_salida"] == 1


@pytest.mark.asyncio
async def test_captura_stderr_y_traceback() -> None:
    """Un proceso que revienta deja su traceback visible, no un fallo mudo."""

    corredor = CorredorPruebas((suite("revienta", "raise ValueError('me rompi')"),))
    corredor.lanzar("revienta", "http://127.0.0.1:8000")
    estado = await esperar(corredor, "revienta")

    assert estado["estado"] == "fallo"
    assert "ValueError" in estado["salida"]
    assert "me rompi" in estado["salida"]


# ==========================================================================
# Estado y concurrencia
# ==========================================================================

@pytest.mark.asyncio
async def test_no_se_relanza_mientras_corre() -> None:
    """Dos clics seguidos en Ejecutar no arrancan dos procesos."""

    lento = "import time; time.sleep(1.2); print('listo')"
    corredor = CorredorPruebas((suite("lento", lento),))

    corredor.lanzar("lento", "http://127.0.0.1:8000")
    primera = corredor._ejecuciones["lento"].tarea

    segunda_respuesta = corredor.lanzar("lento", "http://127.0.0.1:8000")
    assert segunda_respuesta["estado"] == "corriendo"
    assert corredor._ejecuciones["lento"].tarea is primera, "se relanzo el proceso"

    estado = await esperar(corredor, "lento")
    assert estado["estado"] == "ok"


@pytest.mark.asyncio
async def test_una_corrida_nueva_no_arrastra_la_salida_anterior() -> None:
    corredor = CorredorPruebas((suite("dos", "print('primera vuelta')"),))
    corredor.lanzar("dos", "http://127.0.0.1:8000")
    await esperar(corredor, "dos")

    corredor._por_id["dos"].argumentos = ["-c", "print('segunda vuelta')"]
    corredor.lanzar("dos", "http://127.0.0.1:8000")
    estado = await esperar(corredor, "dos")

    assert "segunda vuelta" in estado["salida"]
    assert "primera vuelta" not in estado["salida"], "la salida vieja se quedo pegada"


def test_estado_inicial_es_pendiente() -> None:
    corredor = CorredorPruebas((suite("x", "pass"),))
    assert corredor.estado("x")["estado"] == "pendiente"
    assert corredor.estado("x")["codigo_salida"] is None


# ==========================================================================
# La url solo se pasa a quien la necesita
# ==========================================================================

@pytest.mark.asyncio
async def test_url_se_inyecta_a_quien_la_pide() -> None:
    """Las suites de humo y adversarial hablan con la API por HTTP.

    Se comprueba imprimiendo los argumentos que recibio el proceso: es la unica
    forma de verificar que --url llego de verdad y no solo que lo pusimos en la
    cadena que se muestra en pantalla.
    """

    ver_argumentos = "import sys; print('|'.join(sys.argv[1:]))"
    corredor = CorredorPruebas((suite("conurl", ver_argumentos, necesita_url=True),))
    corredor.lanzar("conurl", "http://127.0.0.1:9999/")
    estado = await esperar(corredor, "conurl")

    # La barra final se recorta: las suites construyen rutas concatenando.
    assert "--url|http://127.0.0.1:9999" in estado["salida"]
    assert not estado["salida"].strip().endswith("9999/")


@pytest.mark.asyncio
async def test_sin_url_no_se_inyecta_nada() -> None:
    ver_argumentos = "import sys; print('ARGS:' + '|'.join(sys.argv[1:]))"
    corredor = CorredorPruebas((suite("sinurl", ver_argumentos, necesita_url=False),))
    corredor.lanzar("sinurl", "http://127.0.0.1:9999")
    estado = await esperar(corredor, "sinurl")

    assert "--url" not in estado["salida"]


# ==========================================================================
# El catalogo real que ve el panel
# ==========================================================================

def test_el_catalogo_real_esta_completo() -> None:
    corredor = CorredorPruebas()
    catalogo = corredor.catalogo()

    assert {s["id"] for s in catalogo} == {
        "motor", "unitarias", "adversarial", "humo", "rag", "tendencia", "cifras",
    }
    for entrada in catalogo:
        assert entrada["titulo"]
        assert entrada["que"], f"{entrada['id']} no explica que comprueba"
        # El comando visible es la promesa de reproducibilidad de la consola:
        # quien no confie en el panel tiene que poder copiarlo y pegarlo.
        assert entrada["comando_visible"].startswith("python ")
        assert entrada["estado"] == "pendiente"


def test_las_suites_que_hablan_con_la_api_lo_declaran() -> None:
    por_id = {s.id: s for s in SUITES}
    assert por_id["humo"].necesita_url is True
    assert por_id["adversarial"].necesita_url is True
    # El replay del motor es puro: ni red ni servidor. Si algun dia necesita
    # url, es que dejo de ser una prueba del motor de reglas.
    assert por_id["motor"].necesita_url is False
    assert por_id["unitarias"].necesita_url is False
