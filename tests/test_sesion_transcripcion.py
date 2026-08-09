"""La sesión de transcripción: qué audio pertenece a qué turno.

`stt/sesion.py` no tenía un solo test, y es el módulo con la lógica más sutil del camino
de voz: decide cuándo arrancar una transcripción antes de que el paciente termine de
hablar, cuándo reutilizarla y cuándo tirarla. Ya tuvo un fallo documentado en su propio
docstring —el buffer se soltaba al final, así que todo lo que el paciente dijera durante
la transcripción se acumulaba en el turno que se estaba cerrando y se borraba después.

Se prueba con un STT falso y determinista. Lo que importa aquí no es la calidad de la
transcripción sino **la contabilidad del audio**, y eso es exactamente lo que un doble
deja medir sin depender de un modelo de 769 M de parámetros.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from centinela.stt.sesion import (  # noqa: E402
    MAX_MUESTRAS_NUEVAS,
    SesionTranscripcion,
)
from centinela.stt.whisper import FRECUENCIA, Transcripcion  # noqa: E402


class STTFalso:
    """Devuelve cuántas muestras le llegaron. Así el texto delata QUÉ audio se uso."""

    def __init__(self, retardo_s: float = 0.0) -> None:
        self.retardo_s = retardo_s
        self.llamadas: list[int] = []

    def transcribir(self, muestras: np.ndarray) -> Transcripcion:
        self.llamadas.append(len(muestras))
        if self.retardo_s:
            time.sleep(self.retardo_s)
        return Transcripcion(
            texto=f"{len(muestras)} muestras",
            ms=1.0,
            duracion_audio_s=len(muestras) / FRECUENCIA,
        )


def pcm(segundos: float) -> np.ndarray:
    return np.zeros(int(FRECUENCIA * segundos), dtype=np.float32)


# ---------------------------------------------------------------- contabilidad del audio

def test_agregar_acumula_y_limpiar_vacia() -> None:
    s = SesionTranscripcion(stt=STTFalso())

    s.agregar(pcm(0.5))
    s.agregar(pcm(0.5))

    assert s.n_muestras == int(FRECUENCIA * 1.0)

    s.limpiar()

    assert s.n_muestras == 0
    assert s.buffer == []


def test_un_trozo_vacio_no_cuenta() -> None:
    """Llega de verdad: el navegador manda tramas vacias al soltar el boton."""

    s = SesionTranscripcion(stt=STTFalso())

    s.agregar(np.array([], dtype=np.float32))

    assert s.n_muestras == 0


# ---------------------------------------------------------------- cuando se especula

@pytest.mark.asyncio
async def test_no_se_especula_sobre_un_audio_demasiado_corto() -> None:
    """Por debajo de 0.4 s no hay nada que transcribir que valga el hilo."""

    s = SesionTranscripcion(stt=STTFalso())
    s.agregar(pcm(0.2))

    assert s.especular() is False


@pytest.mark.asyncio
async def test_se_especula_en_cuanto_hay_audio_suficiente() -> None:
    s = SesionTranscripcion(stt=STTFalso())
    s.agregar(pcm(0.6))

    assert s.especular() is True

    await s.finalizar()


@pytest.mark.asyncio
async def test_no_se_lanzan_dos_especulaciones_sobre_el_mismo_audio() -> None:
    """La segunda no aporta y ocupa el unico hilo que atiende al modelo."""

    s = SesionTranscripcion(stt=STTFalso(retardo_s=0.15))
    s.agregar(pcm(0.6))

    assert s.especular() is True
    assert s.especular() is False, "ya habia una en vuelo"

    await s.finalizar()


# ---------------------------------------------------------------- reutilizar o tirar

@pytest.mark.asyncio
async def test_si_el_paciente_no_dijo_casi_nada_mas_se_reutiliza() -> None:
    """El caso que hace valer la especulacion: llega lista y se sirve."""

    stt = STTFalso()
    s = SesionTranscripcion(stt=stt)
    s.agregar(pcm(1.0))
    s.especular()
    await asyncio.sleep(0.05)          # se deja acabar
    s.agregar(pcm(0.05))               # un pelin mas de audio, por debajo del margen

    res = await s.finalizar()

    assert res.origen == "especulativa"
    assert len(stt.llamadas) == 1, "no se transcribio dos veces"


@pytest.mark.asyncio
async def test_si_el_paciente_siguio_hablando_se_transcribe_entero() -> None:
    """Reutilizar aqui perderia palabras, que es peor que gastar el hilo otra vez."""

    stt = STTFalso()
    s = SesionTranscripcion(stt=stt)
    s.agregar(pcm(1.0))
    s.especular()
    await asyncio.sleep(0.05)
    s.agregar(pcm(2.0))                # muy por encima del margen

    res = await s.finalizar()

    assert res.origen == "completa"
    assert stt.llamadas[-1] == int(FRECUENCIA * 3.0), "la segunda vez va el audio entero"


@pytest.mark.asyncio
async def test_el_margen_de_reutilizacion_es_el_declarado() -> None:
    """Justo en el limite se reutiliza; un pelo por encima, no.

    Se compara contra la constante del modulo y no contra un numero a mano para que las
    dos no puedan divergir.
    """

    async def origen_con(muestras_extra: int) -> str:
        s = SesionTranscripcion(stt=STTFalso())
        s.agregar(pcm(1.0))
        s.especular()
        await asyncio.sleep(0.05)
        if muestras_extra:
            s.agregar(np.zeros(muestras_extra, dtype=np.float32))
        return (await s.finalizar()).origen

    assert await origen_con(MAX_MUESTRAS_NUEVAS) == "especulativa"
    assert await origen_con(MAX_MUESTRAS_NUEVAS + 1) == "completa"


# ---------------------------------------------------------------- el fallo documentado

@pytest.mark.asyncio
async def test_el_audio_que_llega_durante_la_transcripcion_es_del_turno_siguiente() -> None:
    """El fallo que el docstring del modulo describe, convertido en prueba.

    Con barge-in el paciente puede empezar a hablar mientras el agente piensa. Ese audio
    es del turno SIGUIENTE. Antes el buffer se limpiaba al terminar de transcribir, asi
    que esas tramas se acumulaban en el turno que se cerraba y se borraban con el: se
    perdian enteras.
    """

    s = SesionTranscripcion(stt=STTFalso(retardo_s=0.1))
    s.agregar(pcm(1.0))

    cierre = asyncio.create_task(s.finalizar())
    await asyncio.sleep(0.02)          # mientras transcribe...
    s.agregar(pcm(0.7))                # ...el paciente mete baza
    await cierre

    assert s.n_muestras == int(FRECUENCIA * 0.7), (
        "el audio del turno siguiente tiene que sobrevivir al cierre del anterior"
    )


@pytest.mark.asyncio
async def test_texto_especulado_no_consume_la_especulacion() -> None:
    """El cierre adaptativo lo mira para decidir si el paciente ya termino.

    Si al mirarlo la consumiera, `finalizar` tendria que transcribir otra vez y el
    cierre adaptativo pasaria de ahorrar tiempo a costarlo.
    """

    stt = STTFalso()
    s = SesionTranscripcion(stt=stt)
    s.agregar(pcm(1.0))
    s.especular()

    texto = await s.texto_especulado()
    res = await s.finalizar()

    assert texto == res.transcripcion.texto
    assert len(stt.llamadas) == 1, "se transcribio una sola vez para las dos cosas"


@pytest.mark.asyncio
async def test_finalizar_sin_audio_no_revienta() -> None:
    """Una llamada en la que el paciente nunca habla tiene que cerrar igual."""

    s = SesionTranscripcion(stt=STTFalso())

    res = await s.finalizar()

    assert res.transcripcion is not None


# ==========================================================================
# La guarda de "una comprobación de interrupción a la vez"
#
# El modelo atiende de uno en uno, así que dos transcripciones concurrentes no van en
# paralelo: la segunda espera. Medido sobre una llamada con 7.7 s de audio real, el
# detector de barge-in lanzaba una comprobación por veredicto sin mirar si ya había otra
# en vuelo: 55 invocaciones y 172.6 s transcritos — 22 veces el audio que existía — y el
# turno posterior a la interrupción tardaba 7 852 ms cuando transcribir ese mismo audio
# aislado cuesta 480 ms. El turno no era lento: esperaba la cola.
#
# La guarda vive en `main._atender_audio`, que necesita un WebSocket para probarse. Lo
# que se prueba aquí es la propiedad que la hace necesaria y el patrón que la implementa,
# porque es el que ya usaba `especular()` y el que no tenía la comprobación.
# ==========================================================================

def test_comprobar_un_candidato_que_crece_hace_el_trabajo_cuadratico() -> None:
    """La causa medida, reproducida en pequeño.

    El candidato de una interrupción crece mientras el paciente habla. Comprobarlo
    completo en cada veredicto transcribe una y otra vez el mismo audio inicial: la suma
    es cuadrática en el número de comprobaciones, no lineal.

    Un primer intento de test afirmaba que dos `asyncio.to_thread` no van en paralelo, y
    era falso: el executor tiene varios hilos y medido dan 0.121 s para dos de 0.12 s. La
    latencia del turno no venía de un hilo bloqueado sino de **trabajo total**: 172.6 s
    de audio transcrito para 7.7 s reales. Eso es lo que se prueba aquí.
    """

    stt_completo = STTFalso()
    stt_ventana = STTFalso()
    ventana = int(1.8 * FRECUENCIA)
    candidato = np.array([], dtype=np.float32)

    # Veinte veredictos, con el candidato creciendo 0.4 s cada vez: una interrupcion
    # larga, que es donde el crecimiento se nota.
    for _ in range(20):
        candidato = np.concatenate([candidato, pcm(0.4)])
        stt_completo.transcribir(candidato)
        stt_ventana.transcribir(candidato[-ventana:])

    audio_real = len(candidato) / FRECUENCIA
    total_completo = sum(stt_completo.llamadas) / FRECUENCIA
    total_ventana = sum(stt_ventana.llamadas) / FRECUENCIA

    # Sin ventana, la ultima comprobacion mira el candidato entero y el total crece con
    # el cuadrado del numero de veredictos.
    assert max(stt_completo.llamadas) == len(candidato)
    assert total_completo > 5 * audio_real

    # Con ventana, ninguna comprobacion pasa de la ventana y el total crece lineal.
    assert max(stt_ventana.llamadas) == ventana
    assert total_ventana < total_completo


def test_el_patron_de_la_guarda_es_el_mismo_que_ya_usaba_especular() -> None:
    """Una tarea en vuelo se detecta con `is not None and not done()`.

    Se comprueba sobre la guarda real de la sesión —`especular()` la tiene desde el
    principio— para dejar constancia de cuál era el patrón que la comprobación de
    interrupción no seguía.
    """

    import inspect

    from centinela.stt import sesion as mod

    fuente = inspect.getsource(mod.SesionTranscripcion.especular)

    assert "done()" in fuente and "is not None" in fuente
