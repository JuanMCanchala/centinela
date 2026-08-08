"""El WebSocket sobrevive a un navegador con muchas cookies.

Este archivo existe por un fallo que no apunta a su causa por ningun lado, y que con el
barge-in dejo de ser una molestia para apagar una funcion entera.

**El sintoma.** La llamada se abre, los turnos funcionan, y el WebSocket muere con codigo
1006. En el log del servidor solo hay `connection rejected (400 Bad Request)`. Con
`--log-level debug` sale la verdad: `SecurityError: line too long`.

**La causa.** Las cookies se guardan por HOST, no por puerto. Todo lo que corra en
`localhost` -- cualquier otro proyecto del que las tenga -- comparte el mismo tarro.
Medido en una maquina de desarrollo normal: 14 cookies, **9 958 bytes**, de los cuales 9 KB
son tres tokens de Supabase de proyectos que no tienen nada que ver con esto. El navegador
manda ese `Cookie:` de 10 KB en el handshake y `websockets` corta cualquier linea de
cabecera por encima de 8 KB.

**Por que ahora importa.** El turno de voz siempre tuvo respaldo por HTTP, asi que un
WebSocket caido era latencia y nada mas. Ya no: es el unico camino por el que el microfono
llega al servidor mientras el agente habla, asi que sin el no se le puede interrumpir. Y el
jurado va a abrir esto en su maquina, con las cookies que tenga.

**Por que se prueba el efecto y no la intencion.** El primer parche subia una constante que
en esta version de la libreria se llama de otra forma. No dio error: creo un atributo que
nadie lee y el fallo siguio exactamente igual, en silencio. Comprobar que
`MAX_LINE_LENGTH` vale 65536 habria pasado con el parche roto. Lo que se comprueba aqui es
que un handshake con 10 KB de cookies **recibe 101 Switching Protocols**.
"""

from __future__ import annotations

import base64
import os
import socket
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

# Importar `main` es lo que aplica el parche: se hace al importar el modulo.
import centinela.main  # noqa: E402,F401
from centinela.main import TOPE_CABECERA_WS  # noqa: E402

# Lo medido en la maquina de desarrollo, redondeado hacia arriba. Por encima de los 8 KB
# que `websockets` acepta por defecto.
BYTES_DE_COOKIE = 10_000


def _limite_vigente() -> int | None:
    """El tope que la libreria aplica de verdad, con el nombre que use esta version."""

    encontrado = None
    for modulo in ("websockets.legacy.http", "websockets.http11"):
        try:
            mod = __import__(modulo, fromlist=["_"])
        except Exception:  # noqa: BLE001
            mod = None
        if mod is not None:
            for nombre in ("MAX_LINE_LENGTH", "MAX_LINE"):
                valor = getattr(mod, nombre, None)
                if isinstance(valor, int) and encontrado is None:
                    encontrado = valor
    return encontrado


def test_el_parche_toco_la_constante_que_de_verdad_se_lee() -> None:
    """Que exista *alguna* constante subida no basta: tiene que ser la que se lee.

    `websockets.legacy.http.read_line` compara contra una constante concreta. Si el parche
    sube otra, este test sigue viendo el valor bajo.
    """

    vigente = _limite_vigente()
    assert vigente is not None, "no se encontro el tope de linea de la libreria"
    assert vigente >= TOPE_CABECERA_WS, (
        f"el tope vigente es {vigente}, por debajo de {TOPE_CABECERA_WS}: el parche "
        f"subio una constante que nadie lee"
    )


def test_el_tope_cubre_las_cookies_medidas() -> None:
    assert TOPE_CABECERA_WS > BYTES_DE_COOKIE * 2


# ==========================================================================
# El efecto: un handshake real con 10 KB de cookies
# ==========================================================================

def _servidor_de_prueba(puerto_listo: threading.Event, puerto: list[int]) -> None:
    """Levanta el WebSocket de la app en un puerto libre, en un hilo.

    Se usa uvicorn de verdad y no un doble: lo que falla es la libreria de WebSocket que
    uvicorn elige, asi que un doble no probaria nada.
    """

    import uvicorn

    from centinela.main import app

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        puerto[0] = s.getsockname()[1]

    config = uvicorn.Config(
        app, host="127.0.0.1", port=puerto[0], log_level="critical", lifespan="off"
    )
    servidor = uvicorn.Server(config)

    hilo = threading.Thread(target=servidor.run, daemon=True)
    hilo.start()

    for _ in range(200):
        try:
            with socket.create_connection(("127.0.0.1", puerto[0]), timeout=0.2):
                puerto_listo.set()
        except OSError:
            pass
        if puerto_listo.is_set():
            break
    puerto_listo.set()


def _handshake(puerto: int, bytes_de_cookie: int) -> str:
    """Manda un handshake de WebSocket como el de un navegador y devuelve la respuesta."""

    clave = base64.b64encode(os.urandom(16)).decode()
    cookie = "basura=" + ("x" * max(0, bytes_de_cookie - 8))
    lineas = [
        "GET /ws/llamada/no-existe HTTP/1.1",
        f"Host: 127.0.0.1:{puerto}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {clave}",
        "Sec-WebSocket-Version: 13",
        f"Origin: http://127.0.0.1:{puerto}",
        "Sec-WebSocket-Extensions: permessage-deflate; client_max_window_bits",
        f"Cookie: {cookie}",
        "",
        "",
    ]
    with socket.create_connection(("127.0.0.1", puerto), timeout=10) as s:
        s.sendall("\r\n".join(lineas).encode())
        respuesta = s.recv(8192).decode("latin1")
    return respuesta.split("\r\n\r\n")[0]


@pytest.fixture(scope="module")
def puerto_del_servidor() -> int:
    listo = threading.Event()
    puerto: list[int] = [0]
    _servidor_de_prueba(listo, puerto)
    listo.wait(timeout=30)
    return puerto[0]


def test_handshake_con_cookies_de_diez_kilobytes(puerto_del_servidor: int) -> None:
    """La comprobacion que importa. Sin el parche, esto es un 400.

    La ruta apunta a una llamada que no existe a proposito: lo que se prueba es el
    handshake, no el handler. El handler acepta y cierra en cuanto ve que no hay llamada,
    y eso ya es un 101.
    """

    respuesta = _handshake(puerto_del_servidor, BYTES_DE_COOKIE)
    assert "101 Switching Protocols" in respuesta, respuesta[:400]


def test_sin_cookies_tambien_funciona(puerto_del_servidor: int) -> None:
    """El contraste: el caso que ya funcionaba tiene que seguir funcionando."""

    respuesta = _handshake(puerto_del_servidor, 0)
    assert "101 Switching Protocols" in respuesta, respuesta[:400]
