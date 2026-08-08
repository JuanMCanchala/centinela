"""Decidir si el paciente le esta cortando la palabra al agente.

Esta decision vive en el servidor y no en el navegador, y la razon no es de estilo.
Son tres:

  1. **El registro clinico tiene que poder afirmar que oyo el paciente.** Si una
     pregunta se corto a medias, el dominio no se pregunto, y eso cambia el cierre
     (un dominio sin responder nunca da verde). No puede depender de lo que un
     cliente declare.
  2. **Una decision en JavaScript no se puede reproducir.** Aqui toda decision se
     mide con un arnes contra material grabado -- `eval/bargein.py` pasa 53
     locuciones del agente y 18 grabaciones de voz humana por este mismo codigo.
     Un VAD en el navegador solo se puede probar a mano, y a mano no es una medida.
  3. Ya habia un umbral de barge-in en el cliente, fijo en 0.075. Era un numero
     medido en una sola sala. Este se mide en la sala del paciente, cada llamada.

**La idea central.** Mientras el agente habla, lo que el microfono oye ES el eco
residual que la cancelacion del navegador no logro quitar. Eso no es un estorbo: es
una medicion gratis del piso contra el que hay que discriminar. El umbral de
interrupcion se fija por encima de ese eco observado, no por encima de una
constante. En una sala con auriculares el eco es casi nulo y basta un susurro para
interrumpir; con el altavoz a todo volumen el umbral sube solo y hace falta hablar
mas alto -- que es exactamente lo que una persona hace en esa situacion.

**Por que hay ventana de gracia.** El eco tarda en llegar: se manda el audio, el
navegador lo decodifica, el altavoz suena, el microfono lo capta. Durante esos
primeros milisegundos la ventana de eco todavia mide silencio, asi que el umbral
esta bajo y la primera rafaga de eco lo cruzaria. Los primeros 250 ms solo se
aprende, no se sospecha. El coste es real y se declara: no se puede interrumpir al
agente en el primer cuarto de segundo. Tampoco lo hace una persona.

**Por que la energia no confirma nada.** Un portazo, una tos o una silla arrastrada
cruzan cualquier umbral de energia. Este detector no decide interrumpir: decide
*sospechar*. La confirmacion la da la transcripcion del audio que se acumulo durante
la sospecha, con las mismas puertas de calidad que ya filtran las alucinaciones de
Whisper (`stt/whisper.py`). Mientras se comprueba, el agente baja la voz en vez de
callarse -- asi un falso positivo cuesta un bache de 250 ms y no un turno perdido, y
bajar el volumen reduce el eco justo en la ventana donde mas estorba.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

# ==========================================================================
# Punto de operacion
#
# Los tres numeros que deciden se barren en `eval/bargein.py` sobre mezclas de
# locucion del agente + voz humana a atenuaciones de eco conocidas. Lo que se
# publica ahi es donde se rompen, no una afirmacion de que estan bien.
# ==========================================================================

# Cuanto por encima del umbral de voz normal de la sala. Interrumpir cuesta un poco
# mas que hablar en silencio: es deliberado, porque el coste de un corte falso lo
# paga el paciente perdiendo una pregunta.
FACTOR_SOBRE_VOZ = 1.6

# Cuanto por encima del eco observado hay que hablar. Barrido en `eval/bargein.py`
# junto con el percentil de abajo: los dos numeros deciden lo mismo -- lo alto que
# queda el umbral -- y solo tienen sentido mirados juntos.
MARGEN_ECO = 1.8

# Que punto de la distribucion del eco se toma como "el eco". Ni el maximo -- un golpe
# durante la locucion subiria el umbral toda la frase -- ni la mediana, que dejaria las
# silabas fuertes por encima. El barrido mide que 85 con margen 1.8 detecta mas que 95
# con margen 2.2 sin producir ni un corte falso.
PERCENTIL_ECO = 85.0

# Suelo absoluto. Con auriculares el eco es cero y el umbral se iria al piso de la
# sala; por debajo de esto el detector dispararia con la respiracion.
UMBRAL_MINIMO = 0.030

# Racha sostenida. Dos tramas de 64 ms son ~130 ms de voz: un pico aislado del eco o
# un golpe seco no llegan.
TRAMAS_PARA_SOSPECHA = 2

# Ventana de aprendizaje del eco. 24 tramas de 64 ms son ~1.5 s: suficiente para
# cubrir silabas fuertes y flojas de la locucion, corto para seguir un cambio de
# volumen del sistema.
TRAMAS_VENTANA_ECO = 24

# No se sospecha durante este arranque. Ver el encabezado.
MS_GRACIA = 250.0

# Cuanto audio se acumula antes de preguntarle al STT si eso era voz.
MS_CONFIRMACION = 250.0


class Veredicto(str, Enum):
    NADA = "nada"
    # Hay energia sostenida por encima del umbral: baja la voz YA y empieza a
    # acumular. Todavia no se sabe si era el paciente.
    SOSPECHA = "sospecha"
    # La ventana de confirmacion esta llena: transcribe lo acumulado y resuelve.
    COMPROBAR = "comprobar"


# Fases internas. `libre` es el estado en que el agente no tiene la palabra: ahi
# quien abre turno es el VAD del cliente y este detector no opina.
_LIBRE = "libre"
_MIDIENDO = "midiendo"
_SOSPECHA = "sospecha"


@dataclass
class DetectorInterrupcion:
    """Una instancia por llamada. Puro: ni red, ni disco, ni reloj.

    El tiempo entra por parametro (`ms` de cada trama) para que el arnes pueda
    correr una llamada de treinta segundos en un milisegundo y obtener exactamente
    el mismo veredicto que el servidor en vivo.
    """

    # Medidos por el cliente al calibrar la sala y enviados una vez. Son
    # mediciones, no decisiones: el cliente mide, el servidor decide.
    piso_ruido: float = 0.0
    umbral_voz: float = 0.022

    factor_sobre_voz: float = FACTOR_SOBRE_VOZ
    margen_eco: float = MARGEN_ECO
    percentil_eco: float = PERCENTIL_ECO
    umbral_minimo: float = UMBRAL_MINIMO
    tramas_para_sospecha: int = TRAMAS_PARA_SOSPECHA
    ms_gracia: float = MS_GRACIA
    ms_confirmacion: float = MS_CONFIRMACION

    _fase: str = _LIBRE
    _eco: deque[float] = field(default_factory=lambda: deque(maxlen=TRAMAS_VENTANA_ECO))
    _tramas_sobre: int = 0
    _ms_en_fase: float = 0.0
    _emite_voz: bool = True
    # Niveles vistos durante la sospecha. Si se descarta, ese audio ERA eco -- lo
    # acabamos de probar transcribiendolo -- y se aprende. Es lo que hace que el
    # detector converja: un cambio de volumen del sistema cuesta un solo bache de
    # 250 ms, no uno por cada silaba fuerte de la locucion.
    _candidato: list[float] = field(default_factory=list)

    # Lo que disparo la ultima sospecha, para que el log pueda explicar por que.
    rms_disparo: float = 0.0
    umbral_disparo: float = 0.0

    # Contadores de la llamada, para las metricas del turno.
    sospechas: int = 0
    confirmadas: int = 0
    descartadas: int = 0

    # ------------------------------------------------------------------
    # Umbral vigente
    # ------------------------------------------------------------------

    @property
    def eco(self) -> float:
        """Percentil alto del eco observado mientras el agente habla.

        Un percentil y no el maximo: un solo golpe durante la locucion no debe subir
        el umbral para el resto de la frase. Un percentil ALTO y no la mediana: el eco
        de una silaba fuerte tiene que quedar por debajo del umbral, no solo el eco
        promedio.
        """

        if self._eco:
            muestras = sorted(self._eco)
            indice = min(len(muestras) - 1, int(self.percentil_eco / 100.0 * len(muestras)))
            valor = muestras[indice]
        else:
            valor = 0.0
        return valor

    @property
    def umbral(self) -> float:
        return max(
            self.umbral_voz * self.factor_sobre_voz,
            self.eco * self.margen_eco,
            self.umbral_minimo,
        )

    @property
    def sospechando(self) -> bool:
        return self._fase == _SOSPECHA

    @property
    def escuchando_el_suelo(self) -> bool:
        """True mientras el agente tiene la palabra y el detector vigila."""

        return self._fase in (_MIDIENDO, _SOSPECHA)

    # ------------------------------------------------------------------
    # Transiciones que provoca el servidor
    # ------------------------------------------------------------------

    def tomar_la_palabra(self, emite_voz: bool = True) -> None:
        """El agente empieza a hablar (o a pensar la respuesta).

        `emite_voz=False` es el caso de "esta pensando": no hay audio saliendo, asi
        que no hay eco que aprender ni motivo para la ventana de gracia. El paciente
        puede empezar a hablar de inmediato, que es lo que hace cuando el otro
        tarda.
        """

        self._fase = _MIDIENDO
        self._tramas_sobre = 0
        self._ms_en_fase = 0.0
        self._emite_voz = emite_voz
        self._candidato.clear()
        if not emite_voz:
            # Sin audio saliendo lo que el microfono oye es la sala, no el eco. La
            # ventana se vacia para no arrastrar el eco de la locucion anterior:
            # dejarlo puesto mantendria el umbral alto y el paciente no podria
            # meter baza mientras el agente piensa.
            self._eco.clear()

    def soltar_la_palabra(self) -> None:
        self._fase = _LIBRE
        self._tramas_sobre = 0
        self._ms_en_fase = 0.0

    def confirmado(self) -> None:
        """La transcripcion dijo que si era voz. El turno pasa al paciente."""

        self.confirmadas += 1
        self._candidato.clear()
        self.soltar_la_palabra()

    def descartado(self) -> None:
        """Era una tos. El agente recupera la voz y sigue donde iba.

        Los niveles de la sospecha se aprenden como eco: se acaba de comprobar, con
        el STT y no con una heuristica, que ahi no habia voz. Es la correccion que
        hace converger al detector -- sin esto, cada silaba fuerte de la locucion
        provocaria su propio bache.

        La gracia se aplica otra vez porque el volumen vuelve con una rampa y ese
        transitorio cruzaria el umbral recien subido.
        """

        self.descartadas += 1
        for nivel in self._candidato:
            self._eco.append(nivel)
        self._candidato.clear()
        self._fase = _MIDIENDO
        self._tramas_sobre = 0
        self._ms_en_fase = 0.0

    # ------------------------------------------------------------------
    # El bucle
    # ------------------------------------------------------------------

    def observar(self, rms: float, ms: float) -> Veredicto:
        """Una trama de microfono. Devuelve que hacer con ella."""

        veredicto = Veredicto.NADA

        if self._fase == _MIDIENDO:
            self._ms_en_fase += ms
            veredicto = self._vigilar(rms)
        elif self._fase == _SOSPECHA:
            self._ms_en_fase += ms
            self._candidato.append(rms)
            if self._ms_en_fase >= self.ms_confirmacion:
                veredicto = Veredicto.COMPROBAR

        return veredicto

    def _vigilar(self, rms: float) -> Veredicto:
        veredicto = Veredicto.NADA
        en_gracia = self._emite_voz and self._ms_en_fase <= self.ms_gracia

        if en_gracia:
            # Solo se aprende. Es la unica rama que alimenta la ventana con audio
            # que podria estar por encima del umbral, y es a proposito: ese audio
            # ES el eco, y medirlo es justo lo que hace falta.
            self._eco.append(rms)
            self._tramas_sobre = 0
        elif rms > self.umbral:
            self._tramas_sobre += 1
            if self._tramas_sobre >= self.tramas_para_sospecha:
                self.rms_disparo = rms
                self.umbral_disparo = self.umbral
                self.sospechas += 1
                self._fase = _SOSPECHA
                self._ms_en_fase = 0.0
                self._tramas_sobre = 0
                self._candidato = [rms]
                veredicto = Veredicto.SOSPECHA
        else:
            self._tramas_sobre = 0
            # El eco se aprende SOLO con tramas por debajo del umbral. Si se
            # metiera la voz del paciente, el suelo subiria, el umbral con el, y el
            # detector se cegaria a si mismo cuanto mas alto hablara el paciente.
            self._eco.append(rms)

        return veredicto

    # ------------------------------------------------------------------

    def instantanea(self) -> dict:
        """Lo que necesita el log para explicar un corte, sin adivinar nada."""

        return {
            "fase": self._fase,
            "rms_disparo": round(self.rms_disparo, 4),
            "umbral": round(self.umbral, 4),
            "eco_p95": round(self.eco, 4),
            "umbral_voz": round(self.umbral_voz, 4),
            "tramas_eco": len(self._eco),
            "sospechas": self.sospechas,
            "confirmadas": self.confirmadas,
            "descartadas": self.descartadas,
        }


def rms_de(muestras: np.ndarray) -> float:
    """RMS de un bloque float32 en [-1, 1].

    Vive aqui y no en el servidor para que el arnes calcule el nivel exactamente
    igual que la llamada en vivo. En float64 a proposito: en float32 la suma de
    cuadrados de una trama larga y flojita pierde precision justo en el rango que
    decide.
    """

    if len(muestras):
        cuadrados = np.square(np.asarray(muestras, dtype=np.float64))
        valor = float(np.sqrt(cuadrados.mean()))
    else:
        valor = 0.0
    return valor
