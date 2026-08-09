"""Qué hace el agente cuando el paciente se queda callado.

La rúbrica lo pide dentro de *Calidad de la conversación (voz)*: «la latencia de la
conversación (...) y **qué hace tu solución durante los silencios**». Lo que hacía era
nada: el VAD del navegador no cerraba, el turno quedaba abierto, y la llamada duraba hasta
que el barredor de inactividad la cerraba a los 180 s. Tres minutos de nadie hablando y un
registro clínico que no explicaba por qué.

Lo que estas pruebas fijan, y en este orden de importancia:

1. **Que callar siga siendo la primera respuesta.** Pisar la pausa de quien piensa es el
   error que la literatura de agentes de voz nombra por su cuenta, y un paciente en el
   tercer día de un postoperatorio piensa despacio.
2. **Que no se muerda la cola.** El eco de «tómese su tiempo» no puede reiniciar la
   escalera, o el agente diría esa frase cada seis segundos para siempre sin llegar nunca
   al peldaño que cierra.
3. **Que el silencio no escale a rojo.** Callarse no es un síntoma. Y que sí produzca
   seguimiento cuando quedaron dominios sin preguntar, porque no se puede descartar lo
   que no se llegó a preguntar.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))

from centinela.dialog import script as S  # noqa: E402
from centinela.dialog import silencio  # noqa: E402
from centinela.escalation.service import (  # noqa: E402
    AVISO_DE_CIERRE,
    CIERRE_NORMAL,
    CIERRE_SILENCIO,
    CIERRE_SIN_CONTACTO,
)


# ==========================================================================
# 1. Callar es la primera respuesta, no la ausencia de una
# ==========================================================================

@pytest.mark.parametrize("segundos", [0.0, 1.0, 3.0, 5.9])
def test_una_pausa_corta_no_se_pisa(segundos: float) -> None:
    assert silencio.siguiente(segundos, 0) is None


def test_a_los_seis_segundos_acompana_sin_apurar(a_los_seis: float = 6.0) -> None:
    peldano = silencio.siguiente(a_los_seis, 0)
    assert peldano is not None
    assert peldano.accion == silencio.ACOMPANAR


def test_el_primer_peldano_no_pregunta_si_sigue_ahi() -> None:
    """«¿Sigue ahí?» a los seis segundos es una prisa que el agente no tiene derecho a
    tener con alguien dolorido y medicado."""

    assert "sigue" not in S.SILENCIO_ACOMPANAR.texto.lower()
    assert "tómese su tiempo" in S.SILENCIO_ACOMPANAR.texto.lower()


# ==========================================================================
# 2. Cada peldaño una vez, y en su orden
# ==========================================================================

def test_el_mismo_peldano_no_se_repite() -> None:
    """El vigilante mira cada medio segundo. Sin contador, «tómese su tiempo» saldría
    dos veces por segundo mientras el silencio siguiera por encima de 6 s."""

    assert silencio.siguiente(30.0, 0).accion == silencio.ACOMPANAR
    assert silencio.siguiente(30.0, 1).accion == silencio.REPREGUNTAR
    assert silencio.siguiente(30.0, 2).accion == silencio.COMPROBAR_LINEA
    assert silencio.siguiente(30.0, 3).accion == silencio.CERRAR


def test_la_escalera_se_acaba_y_no_da_vueltas() -> None:
    assert silencio.siguiente(1000.0, len(silencio.ESCALERA)) is None


def test_los_peldanos_se_miden_desde_el_anterior_no_desde_el_principio() -> None:
    """Contando desde el principio, el peldaño de los 14 s se cumpliría justo cuando
    termina de sonar el de los 6 s, y el paciente recibiría dos frases seguidas."""

    segundo = silencio.ESCALERA[1]
    assert segundo.espera_s < silencio.segundos_acumulados(1), (
        "la espera relativa no puede ser el total: seria contar desde el principio"
    )


def test_el_total_hasta_cerrar_es_el_que_dice_la_documentacion() -> None:
    assert silencio.segundos_acumulados(0) == 6.0
    assert silencio.segundos_acumulados(1) == 14.0
    assert silencio.segundos_acumulados(2) == 25.0
    assert silencio.segundos_hasta_cerrar() == 40.0


def test_cerrar_es_el_ultimo_peldano_y_solo_el_ultimo() -> None:
    acciones = [p.accion for p in silencio.ESCALERA]
    assert acciones.count(silencio.CERRAR) == 1
    assert acciones[-1] == silencio.CERRAR


def test_la_escalera_no_tarda_mas_que_el_barredor_de_inactividad() -> None:
    """Si tardara más, el barredor cerraría primero con motivo `timeout` y toda esta
    conducta no llegaría a ocurrir nunca."""

    from centinela.config import config

    assert silencio.segundos_hasta_cerrar() < config.timeout_llamada_s


# ==========================================================================
# 3. Las locuciones existen, se pre-renderizan, y no prometen lo que no puede
# ==========================================================================

@pytest.mark.parametrize("locucion", [
    S.SILENCIO_ACOMPANAR, S.SILENCIO_COMPROBAR_LINEA, S.SILENCIO_CIERRE,
])
def test_las_locuciones_del_silencio_estan_en_el_inventario(locucion) -> None:
    """Fuera del inventario no se pre-renderizan, y una locución sin cachear cuesta
    cientos de milisegundos justo cuando el agente quería sonar atento."""

    assert locucion in S.todas_las_locuciones()


def test_el_cierre_por_silencio_no_promete_una_llamada_del_equipo() -> None:
    """Prometer al paciente algo que decide otro es la clase de tranquilización que la
    rúbrica penaliza. Lo que se promete es que queda constancia."""

    texto = S.SILENCIO_CIERRE.texto.lower()
    assert "constancia" in texto
    assert "lo llamar" not in texto and "le llamar" not in texto


def test_ninguna_locucion_del_silencio_lleva_marcador_de_formato() -> None:
    """Con `{nombre}` dentro no es pre-renderizable, y el cache la saltaría en silencio."""

    for loc in (S.SILENCIO_ACOMPANAR, S.SILENCIO_COMPROBAR_LINEA, S.SILENCIO_CIERRE):
        assert "{" not in loc.texto


# ==========================================================================
# 4. El cierre: qué queda registrado, y qué NO
# ==========================================================================

def test_el_silencio_tiene_motivo_propio_distinto_de_una_caida_de_red() -> None:
    """Ante `interrumpida` se asume un fallo técnico; ante esto, que la valoración quedó
    a medias con el paciente al otro lado. Cambia lo que hace quien lo lee."""

    assert CIERRE_SILENCIO not in (CIERRE_NORMAL, CIERRE_SIN_CONTACTO)
    assert CIERRE_SILENCIO in AVISO_DE_CIERRE


def test_el_aviso_explica_que_lo_no_reportado_no_esta_descartado() -> None:
    aviso = AVISO_DE_CIERRE[CIERRE_SILENCIO]
    assert "incompleta" in aviso
    assert "sin preguntar" in aviso


def test_el_cierre_normal_no_pone_aviso() -> None:
    """Decir «terminó bien» en cada hoja es ruido, y el ruido hace que no se lea."""

    assert CIERRE_NORMAL not in AVISO_DE_CIERRE


def test_una_llamada_sin_un_solo_turno_no_entra_en_la_bandeja_clinica() -> None:
    """Guarda estructural sobre `_cerrar_por_silencio`: sin turnos no hay nada que triar,
    y meter eso como AMARILLO es ruido con forma de hallazgo."""

    fuente = (RAIZ / "api" / "centinela" / "main.py").read_text(encoding="utf-8")
    assert "CIERRE_SILENCIO if hubo_contacto else CIERRE_SIN_CONTACTO" in fuente


def test_el_silencio_no_escala_a_rojo_por_su_cuenta() -> None:
    """Callarse no es un síntoma. El nivel lo sigue poniendo el motor de triaje."""

    fuente = (RAIZ / "api" / "centinela" / "main.py").read_text(encoding="utf-8")
    cuerpo = fuente.split("async def _cerrar_por_silencio")[1].split("async def")[0]
    assert "ROJO" not in cuerpo and "rojo" not in cuerpo.replace("por_silencio", "")


# ==========================================================================
# 5. El reloj mide al paciente, no al eco del propio agente
# ==========================================================================

def test_el_reloj_del_silencio_no_cuenta_el_eco_del_agente() -> None:
    """El fallo que este campo evita: el agente dice «tómese su tiempo», el eco de esa
    frase mueve el reloj, la escalera se reinicia, y a los seis segundos lo vuelve a
    decir. Para siempre, sin llegar nunca a cerrar."""

    fuente = (RAIZ / "api" / "centinela" / "main.py").read_text(encoding="utf-8")
    assert "ultima_voz_libre_en" in fuente
    assert "if not (config.bargein and canal.detector.escuchando_el_suelo):" in fuente


def test_el_vigilante_espera_a_que_el_cliente_diga_que_abrio_el_microfono() -> None:
    """El saludo suena por el `<audio>` del cliente, no por el socket: ahí el servidor no
    tiene la palabra y su reloj arrancaría mientras el agente todavía habla."""

    fuente = (RAIZ / "api" / "centinela" / "main.py").read_text(encoding="utf-8")
    assert 'tipo == "escuchando"' in fuente
    assert "canal.escucha_abierta" in fuente

    cliente = (RAIZ / "web" / "app.js").read_text(encoding="utf-8")
    assert 'tipo: "escuchando"' in cliente


def test_se_puede_apagar_para_medir_latencia() -> None:
    """Los arneses que miden latencia no quieren un agente hablando solo entre turnos."""

    from centinela.config import config

    assert hasattr(config, "silencio")


def test_abrir_el_microfono_no_es_lo_mismo_que_hablar() -> None:
    """El fallo que costo una corrida entera, medido: siete «tómese su tiempo» en 45 s.

    El cliente manda `escuchando` tambien despues de oir cada peldaño. Si ese mensaje
    reinicia la escalera, el agente repite el primer peldaño para siempre y no llega
    nunca al que cierra -- justo la conducta que la escalera existe para producir.

    Se comprueba estructuralmente porque el fallo vivia en la relacion entre dos campos:
    `escucha_abierta_en` mueve el hueco que se concede, `ultima_voz_libre_en` reinicia la
    escalera, y solo el segundo lo escribe el audio del paciente.
    """

    fuente = (RAIZ / "api" / "centinela" / "main.py").read_text(encoding="utf-8")
    manejador = fuente.split('tipo == "escuchando"')[1].split("elif tipo ==")[0]

    assert "canal.escucha_abierta_en = time.perf_counter()" in manejador
    assert "ultima_voz_libre_en" not in manejador, (
        "el mensaje de abrir microfono vuelve a reiniciar la escalera de silencio"
    )


# ==========================================================================
# 6. La red por debajo: un cliente que deja de confirmar
# ==========================================================================

def test_el_servidor_suelta_el_suelo_si_el_cliente_no_confirma() -> None:
    """`fin_reproduccion` es la señal buena, y era la única.

    Un navegador que se cuelga a media locución dejaba al agente con la palabra para
    siempre: sin eco que interpretar, sin turno posible, y con el vigilante de silencio
    desactivado -- la red que tenía que cubrir precisamente a quien no responde. Lo
    destapó el arnés de silencio, que no mandaba la confirmación y vio un solo peldaño.
    """

    fuente = (RAIZ / "api" / "centinela" / "main.py").read_text(encoding="utf-8")
    assert "_soltar_suelo_si_el_cliente_callo" in fuente
    assert "MARGEN_CONFIRMACION_S" in fuente


def test_la_red_del_suelo_corre_aunque_la_escalera_este_apagada() -> None:
    """Con `CENTINELA_SILENCIO=0` la conducta ante el silencio se apaga, pero un cliente
    colgado sigue siendo un cliente colgado."""

    fuente = (RAIZ / "api" / "centinela" / "main.py").read_text(encoding="utf-8")
    vigilante = fuente.split("async def _vigilar_silencio")[1].split("@app.websocket")[0]
    orden_red = vigilante.index("_soltar_suelo_si_el_cliente_callo")
    orden_escalera = vigilante.index("config.silencio")

    assert orden_red < orden_escalera, (
        "la red de seguridad quedo dentro de la condicion que la escalera controla"
    )


def test_el_margen_de_confirmacion_se_equivoca_del_lado_de_esperar() -> None:
    """Soltar el suelo antes de tiempo tiene un precio real: el eco de la propia voz del
    agente entraria como turno del paciente."""

    import re

    fuente = (RAIZ / "api" / "centinela" / "main.py").read_text(encoding="utf-8")
    margen = float(re.search(r"MARGEN_CONFIRMACION_S = ([\d.]+)", fuente).group(1))

    assert margen >= 2.0
