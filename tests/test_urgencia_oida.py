"""La instruccion de irse a urgencias es la unica frase que no se puede perder.

Este archivo existe por un fallo que se vio en una demo y que ningun test de entonces
podia atrapar, porque la garantia que faltaba no estaba escrita en ninguna parte.

**El sintoma.** El paciente dice que tiene 38.5 de fiebre. El agente empieza a hablar,
se detiene a media locucion, y la llamada se corta. En el registro esta el texto
completo -- incluido "dirijase al servicio de urgencias mas cercano" -- pero el paciente
no lo oyo.

**La causa.** El JSON del turno viaja ANTES de la voz, a proposito, para que un ticket
rojo no espere a que el agente termine de hablar. El cliente colgaba al recibirlo, y
colgar para los nodos de audio ya programados y cierra el socket por el que venia el
resto de la locucion.

**Lo que se prueba aqui** es la mitad de servidor de esa correccion, mas la garantia
que hacia falta de todas formas: que interrumpir al agente justo en esa frase no la
borra. Con barge-in, el paciente puede callar al agente en el peor momento posible.

Las cuatro propiedades:

  1. La instruccion de urgencias lleva el papel URGENTE, y por eso se debe siempre.
  2. Cortada, no se pierde: vuelve en el turno siguiente y la llamada no cuelga antes.
  3. Se repite UNA vez. Insistir una tercera no informa a nadie.
  4. El registro dice si el paciente la oyo entera, y los proximos pasos cambian si no.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from centinela.clinical.extractor import ResultadoExtraccion  # noqa: E402
from centinela.clinical.normalizer import normalizar_turno  # noqa: E402
from centinela.clinical.triage_engine import TriageEngine  # noqa: E402
from centinela.dialog import script as S  # noqa: E402
from centinela.dialog.policy import (  # noqa: E402
    PAPEL_URGENTE,
    DialogPolicy,
    EstadoLlamada,
    Paciente,
)
from centinela.models import Nivel  # noqa: E402


class ExtractorConFiebre:
    """Pone 38.5 en cuanto el paciente menciona la fiebre. Dispara la bandera roja."""

    async def extraer(self, texto_paciente, estado, turno_idx, pregunta_agente="",
                      dominio_objetivo="", **_):
        norm = normalizar_turno(texto_paciente, dominio_objetivo)
        if "fiebre" in texto_paciente.lower():
            estado.fiebre_c.valor = 38.5
            estado.fiebre_c.conocido = True
        return ResultadoExtraccion(estado=estado, normalizado=norm, respondio=True)


def nueva_policy() -> DialogPolicy:
    policy = DialogPolicy(
        paciente=Paciente(
            paciente_id="pac_test", nombre="Paciente Test",
            procedimiento="Reemplazo de cadera/rodilla", dia_postop=3,
        ),
        extractor=ExtractorConFiebre(),
        motor=TriageEngine(),
    )
    policy.abrir()
    return policy


async def hasta_la_bandera_roja(policy: DialogPolicy):
    """Deja la llamada con la instruccion de urgencias recien dicha.

    Pasan por medio dos turnos y no uno, porque desde `dialog/confirmacion.py` el agente
    lee de vuelta lo que entendio antes de mandar a nadie a urgencias. La alerta ya salio
    en el turno de la bandera; lo que la confirmacion cambia es lo que el paciente oye.
    Eso se prueba en `test_confirmacion.py`; aqui solo hay que atravesarlo.
    """

    await policy.procesar("Si, soy yo")
    await policy.procesar("Si, he tenido fiebre, treinta y ocho punto cinco")
    return await policy.procesar("Si, es correcto")


# ==========================================================================
# 1 · El papel URGENTE existe y esta donde tiene que estar
# ==========================================================================

@pytest.mark.asyncio
async def test_la_instruccion_de_urgencias_es_un_fragmento_urgente() -> None:
    policy = nueva_policy()
    accion = await hasta_la_bandera_roja(policy)

    assert accion.decision.nivel is Nivel.ROJO
    urgentes = [f for f in accion.fragmentos if f.papel == PAPEL_URGENTE]
    assert len(urgentes) == 1, "la instruccion de urgencias, y solo ella"
    assert urgentes[0].clave == S.CIERRE_ROJO.clave


@pytest.mark.asyncio
async def test_el_cierre_rojo_del_final_tambien_es_urgente() -> None:
    """Un rojo descubierto al terminar el cuestionario manda igual a urgencias."""

    policy = nueva_policy()
    policy.estado.fiebre_c.valor = 38.5
    policy.estado.fiebre_c.conocido = True
    policy.escalado = True          # ya se escalo: el cierre no interrumpe, cierra

    fragmentos = policy._cerrar()

    assert policy.fase is EstadoLlamada.TERMINADA
    assert [f.papel for f in fragmentos] == [PAPEL_URGENTE]


def test_el_articulo_del_procedimiento_concuerda() -> None:
    """El agente decia, en voz alta, "despues de una reemplazo de cadera/rodilla"."""

    assert S.articulo_de("Reemplazo de cadera/rodilla") == "un"
    assert S.articulo_de("Colecistectomía") == "una"
    assert S.articulo_de("Apendicectomía") == "una"
    assert S.articulo_de("Mastectomía") == "una"
    assert S.articulo_de("Colectomía") == "una"
    # Los que no estan en el dataset pero pueden entrar mañana.
    assert S.articulo_de("bypass gastrico") == "un"
    assert S.articulo_de("reseccion intestinal") == "una"
    assert S.articulo_de("") == "un"


@pytest.mark.asyncio
async def test_el_texto_de_la_bandera_no_dice_una_reemplazo() -> None:
    policy = nueva_policy()
    accion = await hasta_la_bandera_roja(policy)

    assert "una reemplazo" not in accion.texto_completo
    assert "un reemplazo de cadera/rodilla" in accion.texto_completo


# ==========================================================================
# 2 · Cortada, no se pierde
# ==========================================================================

@pytest.mark.asyncio
async def test_la_urgencia_cortada_queda_en_deuda_y_se_repite() -> None:
    policy = nueva_policy()
    await hasta_la_bandera_roja(policy)

    # Oyo los dos primeros fragmentos; la instruccion, no.
    corte = policy.marcar_interrumpido(2)

    assert corte["urgencia_en_deuda"] is True
    assert policy.urgencia_oida is False
    assert any(f.papel == PAPEL_URGENTE for f in policy.deuda)

    # El paciente habla. La llamada esta TERMINADA y aun asi hay que decirle esto.
    siguiente = await policy.procesar("perdon, que me decia?")

    claves = [f.clave for f in siguiente.fragmentos]
    assert S.RETOMAR_URGENCIA.clave in claves
    assert S.CIERRE_ROJO.clave in claves
    assert siguiente.llamada_terminada is True


@pytest.mark.asyncio
async def test_al_retomar_no_se_reanuda_el_cuestionario() -> None:
    """El expediente esta cerrado. Volver a preguntar por el apetito seria absurdo."""

    policy = nueva_policy()
    await hasta_la_bandera_roja(policy)
    policy.marcar_interrumpido(2)

    siguiente = await policy.procesar("perdon, que me decia?")

    del_guion = [
        f for f in siguiente.fragmentos
        if f.clave and f.clave.endswith(("_inicial", "_reintento", "_profundizar"))
    ]
    assert del_guion == []


@pytest.mark.asyncio
async def test_no_se_escala_dos_veces_por_la_misma_bandera() -> None:
    """`escalado` es lo que impide que el turno de despues vuelva a interrumpir."""

    policy = nueva_policy()
    await hasta_la_bandera_roja(policy)
    policy.marcar_interrumpido(2)

    siguiente = await policy.procesar("perdon, que me decia?")

    assert siguiente.fragmentos[0].clave != S.INTERRUPCION_ROJA.clave
    assert policy.turnos[-1].texto.count("signo de alarma") == 0


# ==========================================================================
# 3 · Se repite una vez, no en bucle
# ==========================================================================

@pytest.mark.asyncio
async def test_la_urgencia_se_repite_una_sola_vez() -> None:
    policy = nueva_policy()
    await hasta_la_bandera_roja(policy)

    policy.marcar_interrumpido(2)
    primera = await policy.procesar("que?")
    assert S.CIERRE_ROJO.clave in [f.clave for f in primera.fragmentos]

    # Corta tambien la repeticion.
    policy.marcar_interrumpido(0)
    segunda = await policy.procesar("no le oigo")

    assert segunda.fragmentos == [], "insistir una tercera vez no informa a nadie"
    assert segunda.llamada_terminada is True
    assert policy.urgencia_oida is False


@pytest.mark.asyncio
async def test_un_turno_vacio_no_ensucia_la_transcripcion() -> None:
    policy = nueva_policy()
    await hasta_la_bandera_roja(policy)
    policy.marcar_interrumpido(2)
    await policy.procesar("que?")
    policy.marcar_interrumpido(0)
    await policy.procesar("no le oigo")

    vacios = [t for t in policy.turnos if not t.texto.strip()]
    assert vacios == []


# ==========================================================================
# 4 · El registro dice si se oyo
# ==========================================================================

@pytest.mark.asyncio
async def test_sin_interrupcion_la_urgencia_se_marca_oida() -> None:
    policy = nueva_policy()
    await hasta_la_bandera_roja(policy)

    assert policy.urgencia_oida is None, "todavia no ha sonado"
    policy.anotar_reproduccion_completa()
    assert policy.urgencia_oida is True


@pytest.mark.asyncio
async def test_una_llamada_verde_no_declara_nada_sobre_la_urgencia() -> None:
    """`None` no es lo mismo que `False`: en una llamada verde no hubo instruccion."""

    policy = nueva_policy()
    await policy.procesar("Si, soy yo")
    policy.anotar_reproduccion_completa()

    assert policy.urgencia_oida is None


def test_los_proximos_pasos_cambian_si_no_se_oyo() -> None:
    from centinela.escalation.service import EscalationService

    motor = TriageEngine()
    estado_rojo = nueva_policy().estado
    estado_rojo.fiebre_c.valor = 39.0
    estado_rojo.fiebre_c.conocido = True
    decision = motor.evaluar(estado_rojo, cerrar=True)

    oida = EscalationService._proximos_pasos(decision, True)
    no_oida = EscalationService._proximos_pasos(decision, False)
    sin_dato = EscalationService._proximos_pasos(decision, None)

    assert "Se indico al paciente acudir a urgencias" in oida
    assert oida == sin_dato, "sin dato se asume el camino normal, como antes"
    assert "NO se alcanzo a decir completa" in no_oida
    assert "interrumpio" in no_oida


# ==========================================================================
# 5 · La mitad de CLIENTE, que es donde el fallo volvio
#
# El resto de este archivo prueba el servidor. La consola tenia su propia mitad sin
# probar, y ahi el fallo volvio a entrar por la puerta de atras: el temporizador que se
# anadio como red de seguridad -- "si `fin_voz` no llega, cuelga igual y di por que" --
# era un plazo FIJO de 20 s desde que el servidor decia `terminada`.
#
# El cierre de una llamada roja son 29.5 s de audio, medidos sobre el WAV que sirve la
# API. Asi que el guardian no era una red: saltaba SIEMPRE y colgaba en el segundo 20,
# justo antes de "dirijase al servicio de urgencias mas cercano o llame al 123". El
# registro lo mostraba como "el audio final no llego completo en 20 s", que se lee como
# un aviso tecnico y era el fallo entero.
#
# Y no se podia ver mirando la cache de audio: `cierre_rojo.wav` son 17.8 s, con 2.2 s
# de margen aparente sobre los 20. Lo que se sirve NO es esa locucion, es una
# concatenacion que le suma los hallazgos del paciente, generados en el turno. Por eso
# ningun plazo fijo puede ser correcto: la duracion de lo que se va a decir no se conoce
# al armar el temporizador.
#
# Lo que se comprueba aqui es la propiedad, no la constante: el guardian del cliente se
# renueva cuando la voz AVANZA. Es una prueba de codigo fuente porque la consola no
# tiene arnes de JavaScript, y vale la pena de todas formas: lo que fallo fue una
# decision de diseno que se puede leer.
# ==========================================================================

RUTA_APP_JS = Path(__file__).resolve().parents[1] / "web" / "app.js"


def test_el_guardian_del_cliente_no_es_un_plazo_por_duracion_total() -> None:
    fuente = RUTA_APP_JS.read_text(encoding="utf-8")

    assert "MS_GUARDIAN_COLGADO" not in fuente, (
        "volvio el plazo fijo desde `terminada`: corta el cierre rojo a mitad"
    )
    assert "MS_SIN_AVANCE_DE_VOZ" in fuente


def test_el_guardian_se_renueva_cuando_la_voz_avanza() -> None:
    """Los tres avances: se programa un trozo, termina un trozo, y el `<audio>` corre.

    Los dos primeros son la cola de Web Audio (la voz por WebSocket) y el tercero es el
    camino por HTTP y el turno por texto, que suenan por el elemento `<audio>`. Si
    faltara el tercero, el cierre rojo por texto se seguiria cortando.
    """

    fuente = RUTA_APP_JS.read_text(encoding="utf-8")

    # Una es la definicion; el resto son las renovaciones.
    assert fuente.count("renovarGuardianColgado") >= 5, (
        "falta alguna senal de avance: con menos de tres, hay un camino que se corta"
    )
    assert 'addEventListener("timeupdate", renovarGuardianColgado)' in fuente, (
        "sin `timeupdate` el camino por HTTP y el turno por texto se cortan igual"
    )


def test_el_plazo_sin_avance_es_mas_corto_que_la_locucion_mas_larga() -> None:
    """La comprobacion que ata el numero a la realidad medida.

    Si el plazo fuera mayor que una locucion, seria un plazo por duracion disfrazado. Y
    si alguien lo convierte otra vez en un plazo total, este numero -- por debajo de la
    locucion mas corta del guion -- deja de tener sentido y hay que volver aqui.
    """

    import re
    import wave

    fuente = RUTA_APP_JS.read_text(encoding="utf-8")
    ms = int(re.search(r"MS_SIN_AVANCE_DE_VOZ = (\d+)", fuente).group(1))

    cache = Path(__file__).resolve().parents[1] / "data" / "audio_cache"
    duraciones = []
    for archivo in cache.glob("*.wav"):
        with wave.open(str(archivo)) as w:
            duraciones.append(w.getnframes() / w.getframerate())

    if duraciones:
        mas_larga = max(duraciones)
        assert ms / 1000 < mas_larga, (
            f"el plazo sin avance ({ms/1000:.0f} s) supera la locucion mas larga "
            f"({mas_larga:.1f} s): vuelve a ser un plazo por duracion"
        )
