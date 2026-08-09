"""Qué hace el agente cuando el paciente se queda callado.

La rúbrica lo nombra dentro de *Calidad de la conversación (voz)*: «la latencia de la
conversación (...) **y qué hace tu solución durante los silencios**». Lo que hacía era
nada: el VAD del navegador no cerraba nunca, el turno se quedaba abierto, y la llamada
duraba hasta que el barredor de inactividad la cerraba a los 180 s. Tres minutos de nadie
diciendo nada, y un registro clínico que no explicaba por qué.

**Callar es la primera respuesta correcta, no la ausencia de una.** Un paciente en el
tercer día de un postoperatorio piensa despacio: le duele, está medicado, y muchas veces
es mayor. Hablar encima de esa pausa es el error que la literatura de agentes de voz
nombra por su cuenta —el agente debe callar, o acompañar, nunca pisar la pausa de quien
piensa—. Así que el primer peldaño de esta escalera es esperar.

Los peldaños, y por qué cada uno:

| Espera | Total | Qué hace | Por qué |
|---:|---:|---|---|
| 6 s | 6 s | nada, y luego acompaña | Antes de eso es una pausa, no un silencio. Pisarla es el error. |
| +8 s | 14 s | repregunta | Puede que la pregunta no se entendiera. Se reformula la misma, no otra. |
| +11 s | 25 s | comprueba la línea | Ya no parece que esté pensando. Puede ser el teléfono. |
| +15 s | 40 s | cierra | Y **deja constancia de que la valoración quedó incompleta**. |

Los peldaños se miden **desde el anterior**, no desde que empezó el silencio, y el reloj
se reinicia cada vez que uno se dispara. La diferencia no es cosmética: contando desde el
principio, el peldaño de los 14 s se cumpliría en el instante en que termina de sonar el
de los 6 s —la frase dura lo que dura— y el paciente recibiría dos intervenciones
seguidas sin tiempo para contestar a la primera.

Y lo que se cuenta es **silencio del paciente**, no tiempo de reloj: lo que el agente
tarda en decir cada peldaño no cuenta contra el siguiente. Así que la columna de totales
son 40 s *de silencio*, y una llamada real que recorra la escalera entera dura algo más,
lo que ocupen las tres frases. Contar el tiempo en que el agente habla como si el paciente
estuviera callado sería cobrarle al paciente el turno del agente.

El último peldaño es el que importa clínicamente, y no es una decisión de conversación:
**un paciente que deja de responder no es una llamada que salió bien.** El motor de
triaje ya cierra en AMARILLO cuando quedan dominios sin preguntar —«no se puede descartar
lo que no se llegó a preguntar»—, así que el silencio hereda esa conducta sin inventar una
alerta nueva. Lo que este módulo añade es el motivo: la hoja de traspaso dice que el
paciente dejó de responder, y eso cambia lo que hace la enfermera. No es lo mismo que se
cortara la red.

Y lo que **no** hace, dicho para que no se dé por hecho: no escala a rojo por callarse.
Un silencio no es un síntoma. Escalar rojo aquí sería un falso positivo caro justo en el
criterio donde la rúbrica pesa la asimetría clínica al contrario.

El módulo es puro y sin E/S, como `completitud.py`: decide el peldaño, no lo ejecuta.
"""

from __future__ import annotations

from dataclasses import dataclass

# Las acciones. Cadenas y no un Enum porque viajan al registro y al JSON del canal, y
# ahi un Enum solo anade una conversion en cada extremo.
ESPERAR = "esperar"
ACOMPANAR = "acompanar"
REPREGUNTAR = "repreguntar"
COMPROBAR_LINEA = "comprobar_linea"
CERRAR = "cerrar"


@dataclass(frozen=True)
class Peldano:
    """Un escalón: cuánto se espera **desde el anterior** y qué se hace al cumplirse."""

    espera_s: float
    accion: str


ESCALERA: tuple[Peldano, ...] = (
    Peldano(6.0, ACOMPANAR),
    Peldano(8.0, REPREGUNTAR),
    Peldano(11.0, COMPROBAR_LINEA),
    Peldano(15.0, CERRAR),
)


def siguiente(desde_el_anterior_s: float, ya_dados: int) -> Peldano | None:
    """El peldaño que toca ahora, o None si toca callar.

    `ya_dados` cuenta los peldaños que ya se usaron en este silencio, y se reinicia en
    cuanto el paciente habla. Sin ese contador, el peldaño de los 6 s se dispararía en
    cada vuelta del vigilante mientras el silencio siguiera por encima de 6 s: el agente
    repetiría «tómese su tiempo» cuatro veces por segundo, que es peor que el silencio.
    """

    toca = None
    if 0 <= ya_dados < len(ESCALERA):
        candidato = ESCALERA[ya_dados]
        if desde_el_anterior_s >= candidato.espera_s:
            toca = candidato
    return toca


def segundos_acumulados(hasta: int) -> float:
    """Cuánto silencio total hace falta para llegar al peldaño `hasta` (0-indexado).

    Es la cifra que se documenta y la que se prueba: los peldaños se escriben como
    esperas relativas porque así es como se ejecutan, y se leen como totales porque así
    es como se razona sobre ellos.
    """

    return sum(p.espera_s for p in ESCALERA[: hasta + 1])


def segundos_hasta_cerrar() -> float:
    """El total que se tolera antes de cerrar. Con la escalera de serie, 40 s."""

    return segundos_acumulados(len(ESCALERA) - 1)
