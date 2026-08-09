"""El token de acceso: que proteja lo que modifica y no lo que se audita.

Dos propiedades que tienen que sostenerse juntas, y que se contradicen si no se
piensan:

  1. **Sin token configurado el sistema queda abierto.** No es descuido: la compuerta
     G2 del reto da quince minutos para levantar la solucion siguiendo solo el README,
     y pedir configurar un secreto para ver la consola gasta ese presupuesto en nada.

  2. **Con token configurado, protege lo que MODIFICA algo** -- subir y borrar
     documentos, atender una alerta, correr las suites, y la llamada ENTERA: abrirla,
     cada turno, el audio, el cierre y el canal de voz -- y deja abierto lo que solo
     lee. `/api/reglas`, `/api/salud` y `/api/metricas` son la parte auditable de la
     entrega; detras de un secreto no la sirven.

Que la llamada este protegida entera es la correccion de un hueco que esta misma lista
tenia: cubria `POST /api/llamadas` y se olvidaba de `/turno`, `/audio`, `/cerrar` y el
WebSocket, asi que el `llamada_id` hacia de credencial de facto. Y no lo era: el
listado `GET /api/llamadas`, de lectura y abierto a proposito, lo entrega. Medido
contra el servidor en marcha, sin presentar nada: se leyo el id de una llamada EN
CURSO, se la condujo a ROJO y se creo su ticket. `test_conducir_una_llamada_...` es el
que impide que vuelva.

Lo que esto NO es: identidad. Un sistema clinico real necesita saber QUE PERSONA
atendio cada alerta, y un secreto compartido no lo puede saber. Esta declarado como
frontera de alcance en docs/operacion.md, no disimulado.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


def cliente_con_token(monkeypatch, token: str) -> TestClient:
    """Recarga la configuracion con el token puesto.

    `config` se construye al importar el modulo, asi que cambiar la variable de
    entorno despues no tiene efecto: hay que reimportar.
    """

    monkeypatch.setenv("CENTINELA_TOKEN", token)
    import centinela.config as cfg

    importlib.reload(cfg)
    import centinela.main as main

    monkeypatch.setattr(main, "config", cfg.config)
    # El registro de llamadas en curso, vacio. Es lo unico que se siembra, y solo para
    # que el canal de voz pueda llegar a su propio "llamada no encontrada" en vez de
    # reventar con KeyError: sin eso no se puede distinguir "paso la puerta" de "fallo".
    # Con `setitem` para que no se quede puesto al terminar la prueba.
    monkeypatch.setitem(main.E, "llamadas", {})
    # Sin `with`: el lifespan levanta tres modelos y aca solo se prueba el acceso.
    #
    # `raise_server_exceptions=False` es necesario justamente por eso: sin lifespan,
    # un endpoint que pase la puerta y llegue al estado del proceso levanta KeyError,
    # y con el valor por defecto el cliente lo relanzaria en vez de devolver un 500.
    # Lo que se mide aca es el codigo de la puerta, no lo que hay detras.
    return TestClient(main.app, raise_server_exceptions=False)


# Endpoints que modifican algo, con un cuerpo minimo que no llega a ejecutarse: la
# dependencia de acceso corre ANTES de validar el cuerpo.
QUE_MODIFICAN = [
    ("post", "/api/llamadas", {"json": {"paciente_id": "x", "nombre": "x",
                                        "procedimiento": "x", "dia_postop": 1}}),
    ("post", "/api/tickets/TK-x-R/atender", {"json": {"quien": "x"}}),
    ("delete", "/api/documentos/inexistente", {}),
    # Conducir una llamada tambien modifica: escribe en el registro clinico de un
    # paciente y puede crear su alerta. Un `llamada_id` cualquiera basta para la
    # prueba, porque la puerta corre antes de buscar la llamada.
    ("post", "/api/llamadas/abc123/turno", {"json": {"texto": "hola"}}),
    ("post", "/api/llamadas/abc123/cerrar", {}),
    ("post", "/api/llamadas/abc123/audio", {"content": b"\x00\x00"}),
]

# Endpoints de solo lectura. La auditabilidad es parte de la entrega.
QUE_SOLO_LEEN = ["/api/salud", "/api/reglas", "/api/metricas"]


@pytest.mark.parametrize("metodo, ruta, extra", QUE_MODIFICAN)
def test_sin_token_lo_que_modifica_da_401(monkeypatch, metodo, ruta, extra) -> None:
    cli = cliente_con_token(monkeypatch, "secreto-de-prueba")

    r = getattr(cli, metodo)(ruta, **extra)

    assert r.status_code == 401, f"{metodo.upper()} {ruta} quedo sin proteger"


@pytest.mark.parametrize("metodo, ruta, extra", QUE_MODIFICAN)
def test_con_un_token_equivocado_tambien_da_401(monkeypatch, metodo, ruta, extra) -> None:
    cli = cliente_con_token(monkeypatch, "secreto-de-prueba")

    r = getattr(cli, metodo)(
        ruta, headers={"Authorization": "Bearer otra-cosa"}, **extra
    )

    assert r.status_code == 401


@pytest.mark.parametrize("metodo, ruta, extra", QUE_MODIFICAN)
def test_con_el_token_correcto_la_peticion_pasa_la_puerta(
    monkeypatch, metodo, ruta, extra
) -> None:
    """Pasar la puerta no es responder 200: detras puede fallar por otras razones.

    Lo que se comprueba es que el 401 desaparece. Sin el lifespan levantado, un
    endpoint que llegue al estado del proceso fallara de otra forma, y eso esta bien:
    significa que el control de acceso ya lo dejo pasar.
    """

    cli = cliente_con_token(monkeypatch, "secreto-de-prueba")

    r = getattr(cli, metodo)(
        ruta, headers={"Authorization": "Bearer secreto-de-prueba"}, **extra
    )

    assert r.status_code != 401


@pytest.mark.parametrize("ruta", QUE_SOLO_LEEN)
def test_lo_que_solo_lee_no_queda_detras_del_token(monkeypatch, ruta) -> None:
    """Un `/api/reglas` protegido no sirve para auditar."""

    cli = cliente_con_token(monkeypatch, "secreto-de-prueba")

    assert cli.get(ruta).status_code != 401


def test_sin_token_configurado_no_se_pide_nada(monkeypatch) -> None:
    """El caso por defecto, y el del arranque cronometrado del jurado."""

    cli = cliente_con_token(monkeypatch, "")

    r = cli.post("/api/tickets/TK-x-R/atender", json={"quien": "x"})

    assert r.status_code != 401


# ==========================================================================
# El hueco que existia: el id como credencial
# ==========================================================================

def test_el_listado_de_llamadas_entrega_el_id(monkeypatch) -> None:
    """Por que el `llamada_id` no puede hacer de credencial.

    Se comprueba sobre el contrato del endpoint abierto, no sobre datos: si algun dia
    dejara de publicar el id, protegerlo dejaria de ser urgente -- pero seguiria siendo
    correcto, porque conducir una llamada modifica el registro de un paciente.
    """

    import centinela.escalation.service as servicio
    import inspect

    fuente = inspect.getsource(servicio.EscalationService.llamadas)

    assert "llamada_id" in fuente, (
        "si el listado abierto ya no publica el id, actualizar este razonamiento"
    )


def test_conducir_una_llamada_pide_el_token(monkeypatch) -> None:
    """La prueba del hueco, junta: abrir NO alcanza para conducir.

    Antes esto pasaba: `POST /api/llamadas` daba 401 y los tres siguientes seguian
    abiertos, asi que quien leyera un id del listado conducia la llamada de otro.
    """

    cli = cliente_con_token(monkeypatch, "secreto-de-prueba")
    conducir = [
        ("post", "/api/llamadas/de-otro/turno", {"json": {"texto": "tengo fiebre"}}),
        ("post", "/api/llamadas/de-otro/audio", {"content": b"\x00" * 3200}),
        ("post", "/api/llamadas/de-otro/cerrar", {}),
    ]

    codigos = {
        ruta: getattr(cli, metodo)(ruta, **extra).status_code
        for metodo, ruta, extra in conducir
    }

    assert set(codigos.values()) == {401}, codigos


# ==========================================================================
# El canal de voz
#
# El navegador no puede poner cabeceras en un WebSocket, asi que el token viaja en el
# subprotocolo. Lo que se prueba aqui es que la puerta existe y que el handshake se
# rechaza ANTES de aceptar: un canal abierto a quien no pasa la puerta, aunque se
# cierre a continuacion, ya recibio audio.
# ==========================================================================

def test_el_canal_de_voz_sin_token_no_se_abre(monkeypatch) -> None:
    cli = cliente_con_token(monkeypatch, "secreto-de-prueba")

    with pytest.raises(WebSocketDisconnect) as caido:
        with cli.websocket_connect("/ws/llamada/de-otro"):
            pass

    assert caido.value.code == 1008


def test_el_canal_de_voz_con_el_token_equivocado_no_se_abre(monkeypatch) -> None:
    cli = cliente_con_token(monkeypatch, "secreto-de-prueba")

    with pytest.raises(WebSocketDisconnect) as caido:
        with cli.websocket_connect(
            "/ws/llamada/de-otro", subprotocols=["centinela.token.otra-cosa"]
        ):
            pass

    assert caido.value.code == 1008


def test_el_canal_de_voz_con_el_token_correcto_pasa_la_puerta(monkeypatch) -> None:
    """Pasar la puerta no es tener llamada: detras contesta que no la encuentra.

    Es justo la senal que se busca. Sin el lifespan levantado `E["llamadas"]` esta
    vacio, asi que el canal se abre, dice `llamada no encontrada` y se cierra -- y eso
    solo puede ocurrir si el control de acceso ya lo dejo pasar.
    """

    cli = cliente_con_token(monkeypatch, "secreto-de-prueba")

    with cli.websocket_connect(
        "/ws/llamada/de-otro", subprotocols=["centinela.token.secreto-de-prueba"]
    ) as ws:
        primero = ws.receive_json()

    assert primero["tipo"] == "error"
    assert "no encontrada" in primero["mensaje"]


def test_sin_token_configurado_el_canal_de_voz_no_pide_subprotocolo(monkeypatch) -> None:
    """El caso del jurado: el cliente no ofrece nada y el canal se abre igual."""

    cli = cliente_con_token(monkeypatch, "")

    with cli.websocket_connect("/ws/llamada/de-otro") as ws:
        primero = ws.receive_json()

    assert primero["tipo"] == "error"


def test_un_token_con_espacios_se_declara_intransportable(monkeypatch) -> None:
    """El aviso de arranque, que evita el fallo en silencio.

    Un token con un espacio no cabe en el subprotocolo. Sin este aviso el sintoma seria
    un canal de voz que no conecta -- y por tanto una llamada sin barge-in -- con el
    codigo 1008 como unica pista.
    """

    monkeypatch.setenv("CENTINELA_TOKEN", "dos palabras")
    import centinela.config as cfg

    importlib.reload(cfg)

    assert cfg.config.token_consola == "dos palabras"
    assert not cfg.config.token_transportable()

    monkeypatch.setenv("CENTINELA_TOKEN", "una-sola-palabra_42")
    importlib.reload(cfg)

    assert cfg.config.token_transportable()
