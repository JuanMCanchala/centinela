"""El token de acceso: que proteja lo que modifica y no lo que se audita.

Dos propiedades que tienen que sostenerse juntas, y que se contradicen si no se
piensan:

  1. **Sin token configurado el sistema queda abierto.** No es descuido: la compuerta
     G2 del reto da quince minutos para levantar la solucion siguiendo solo el README,
     y pedir configurar un secreto para ver la consola gasta ese presupuesto en nada.

  2. **Con token configurado, protege lo que MODIFICA algo** -- subir y borrar
     documentos, abrir una llamada, atender una alerta, correr las suites -- y deja
     abierto lo que solo lee. `/api/reglas`, `/api/salud` y `/api/metricas` son la
     parte auditable de la entrega; detras de un secreto no la sirven.

Lo que esto NO es: identidad. Un sistema clinico real necesita saber QUE PERSONA
atendio cada alerta, y un secreto compartido no lo puede saber. Esta declarado como
frontera de alcance en docs/operacion.md, no disimulado.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


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
