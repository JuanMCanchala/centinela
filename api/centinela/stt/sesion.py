"""Transcripcion especulativa: empezar a transcribir antes de que el turno cierre.

El problema que resuelve es de percepcion, y es el que decide si una llamada se
siente fluida o no.

Medido en esta maquina, la transcripcion es la etapa dominante del turno: 1400 ms
para 3.6 s de audio. Si se espera a que el turno cierre para empezar, el paciente
calla y oye silencio durante todo ese tiempo.

**La idea.** Whisper no es un modelo de streaming -- procesa ventanas de 30 s como
una unidad --, asi que no se puede transcribir palabra por palabra. Pero si se
puede *empezar antes*. El cliente detecta una pausa corta (350 ms) mucho antes de
declarar el fin del turno (900 ms), y en esa pausa manda `pausa_corta`. El
servidor arranca la transcripcion ahi mismo, de forma especulativa.

Cuando llega `fin_habla`, 550 ms mas tarde, hay tres desenlaces:

  1. La transcripcion especulativa ya termino y el paciente no dijo nada nuevo
     desde la pausa -> se reutiliza. Coste percibido: **0 ms**.
  2. Todavia esta corriendo -> se espera lo que le quede, no los 1400 ms enteros.
  3. El paciente siguio hablando tras la pausa -> se descarta y se transcribe
     completo. Se pierde el trabajo especulativo, pero no se pierde tiempo:
     corrio en un hilo aparte mientras el paciente hablaba.

El caso 3 no es un fracaso del diseno: una pausa de 350 ms en mitad de una frase
es normalisima -- "el dolor esta... como en un seis" -- y por eso el umbral de
cierre son 900 ms y no 350. La especulacion es gratis porque ocurre en tiempo que
de todas formas se estaba gastando en escuchar.

**Por que no reconciliar parciales al estilo whisper_streaming.** Esa tecnica
transcribe una ventana deslizante y fusiona los solapes con una politica de
acuerdo local. Da subtitulos en vivo, pero introduce reescrituras: el texto
cambia mientras se muestra. Aqui el consumidor de la transcripcion no es una
pantalla, es un extractor clinico que decide si escalar. Un dato que se reescribe
es peor que un dato que llega 300 ms mas tarde.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import numpy as np

from .whisper import FRECUENCIA, Transcripcion, WhisperSTT

# Cuanto audio nuevo tolera la reutilizacion de una transcripcion especulativa.
# Por debajo de este umbral se considera que el paciente no dijo nada mas: son
# 240 ms, menos que una silaba.
MAX_MUESTRAS_NUEVAS = int(0.24 * FRECUENCIA)

# Espera maxima por una especulacion en curso antes de rendirse y transcribir de
# nuevo. Si tarda mas que esto, algo va mal y es mejor no encadenar la demora.
TIMEOUT_ESPECULACION_S = 6.0


@dataclass
class ResultadoSesion:
    transcripcion: Transcripcion
    # De donde salio el texto, para que las metricas no mientan sobre la latencia.
    origen: str = "completa"          # especulativa | completa | espera_especulativa
    ms_ahorrados: float = 0.0
    muestras_nuevas: int = 0


@dataclass
class SesionTranscripcion:
    """Estado de audio de una llamada. Una instancia por llamada."""

    stt: WhisperSTT
    buffer: list[np.ndarray] = field(default_factory=list)
    n_muestras: int = 0

    # Especulacion en vuelo.
    _tarea: asyncio.Task | None = None
    _muestras_al_especular: int = 0
    _t_inicio_especulacion: float = 0.0
    _ms_especulacion: float = 0.0

    # ------------------------------------------------------------------

    def agregar(self, pcm: np.ndarray) -> None:
        if len(pcm):
            self.buffer.append(pcm)
            self.n_muestras += len(pcm)

    def limpiar(self) -> None:
        self.buffer.clear()
        self.n_muestras = 0
        self._cancelar_especulacion()

    def _audio(self) -> np.ndarray:
        if not self.buffer:
            salida = np.array([], dtype=np.float32)
        elif len(self.buffer) == 1:
            salida = self.buffer[0]
        else:
            salida = np.concatenate(self.buffer)
            # Se consolida para no re-concatenar en cada llamada.
            self.buffer = [salida]
        return salida

    # ------------------------------------------------------------------

    def especular(self) -> bool:
        """Arranca la transcripcion sin esperar el cierre del turno.

        Devuelve False si no habia nada que hacer -- audio insuficiente, o ya hay
        una especulacion en vuelo sobre practicamente el mismo audio.
        """

        arrancada = False
        suficiente = self.n_muestras >= int(0.4 * FRECUENCIA)
        ya_en_vuelo = self._tarea is not None and not self._tarea.done()

        if suficiente and not ya_en_vuelo:
            audio = self._audio().copy()
            self._muestras_al_especular = self.n_muestras
            self._t_inicio_especulacion = time.perf_counter()
            self._tarea = asyncio.create_task(self._transcribir(audio))
            arrancada = True

        return arrancada

    async def _transcribir(self, audio: np.ndarray) -> Transcripcion:
        # `to_thread` es lo que hace que la especulacion sea gratis: Whisper corre
        # en un hilo aparte mientras el bucle de eventos sigue recibiendo el audio
        # que el paciente todavia esta produciendo.
        trans = await asyncio.to_thread(self.stt.transcribir, audio)
        self._ms_especulacion = (time.perf_counter() - self._t_inicio_especulacion) * 1000
        return trans

    def _cancelar_especulacion(self) -> None:
        if self._tarea is not None and not self._tarea.done():
            self._tarea.cancel()
        self._tarea = None
        self._muestras_al_especular = 0

    # ------------------------------------------------------------------

    async def texto_especulado(self) -> str:
        """Texto de la especulacion en vuelo, SIN consumirla.

        Lo usa el cierre adaptativo del turno: mira lo que el paciente lleva dicho
        para decidir si ya termino de contestar, y `finalizar` sigue pudiendo
        reutilizar la misma especulacion despues. Por eso el `shield`: si esta
        espera se agota, la transcripcion de verdad no se puede quedar sin trabajo
        hecho.
        """

        texto = ""
        if self._tarea is not None:
            try:
                trans = await asyncio.wait_for(
                    asyncio.shield(self._tarea), timeout=TIMEOUT_ESPECULACION_S
                )
                texto = trans.texto
            except (asyncio.TimeoutError, asyncio.CancelledError):
                texto = ""
        return texto

    async def finalizar(self) -> ResultadoSesion:
        """Devuelve la transcripcion del turno, reutilizando la especulacion si sirve.

        **El buffer se suelta al principio y no al final**, y eso no es un detalle de
        orden. Con barge-in el paciente puede empezar a hablar mientras el agente
        piensa la respuesta, y esas tramas son el turno SIGUIENTE. Antes el buffer se
        limpiaba al terminar, asi que todo lo que el paciente dijera durante los ~1.4 s
        de transcripcion se acumulaba en el turno que se estaba cerrando y se borraba
        despues: se perdia entero. Aqui se toma una instantanea del audio, se suelta el
        buffer, y lo que llegue despues pertenece a quien le toca.
        """

        nuevas = self.n_muestras - self._muestras_al_especular
        sirve = self._tarea is not None and 0 <= nuevas <= MAX_MUESTRAS_NUEVAS

        audio = self._audio()
        tarea = self._tarea
        self._tarea = None
        self.buffer.clear()
        self.n_muestras = 0
        self._muestras_al_especular = 0

        if not sirve:
            # El paciente siguio hablando: la especulacion cubre solo una parte y
            # reutilizarla perderia palabras. Se descarta y se transcribe entero.
            if tarea is not None and not tarea.done():
                tarea.cancel()
            trans = await asyncio.to_thread(self.stt.transcribir, audio)
            resultado = ResultadoSesion(
                transcripcion=trans,
                origen="completa",
                ms_ahorrados=0.0,
                muestras_nuevas=max(0, nuevas),
            )
        elif tarea.done():
            # El mejor caso: ya estaba lista antes de que el turno cerrara.
            trans = tarea.result()
            resultado = ResultadoSesion(
                transcripcion=trans,
                origen="especulativa",
                ms_ahorrados=round(self._ms_especulacion, 1),
                muestras_nuevas=max(0, nuevas),
            )
        else:
            # Va a medias: se espera lo que le quede, que es menos que empezar.
            t0 = time.perf_counter()
            try:
                trans = await asyncio.wait_for(tarea, timeout=TIMEOUT_ESPECULACION_S)
                ms_esperados = (time.perf_counter() - t0) * 1000
                resultado = ResultadoSesion(
                    transcripcion=trans,
                    origen="espera_especulativa",
                    ms_ahorrados=round(max(0.0, self._ms_especulacion - ms_esperados), 1),
                    muestras_nuevas=max(0, nuevas),
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                if not tarea.done():
                    tarea.cancel()
                trans = await asyncio.to_thread(self.stt.transcribir, audio)
                resultado = ResultadoSesion(
                    transcripcion=trans, origen="completa", muestras_nuevas=max(0, nuevas)
                )

        return resultado
