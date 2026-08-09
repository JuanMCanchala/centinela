"""Saber si el paciente ya termino de contestar, sin esperar a que se calle.

El problema es de ritmo, y es lo que separa una llamada de un walkie-talkie. Hoy el
turno cierra tras 900 ms de silencio, fijos. Una persona contesta en unos 200 ms, asi
que cada respuesta del agente llega con casi un segundo de retraso perceptible.

Bajar los 900 ms a secas seria un error: una pausa de medio segundo en mitad de una
frase es normalisima -- "el dolor esta... como en un seis" -- y cortar ahi convierte
la respuesta en un fragmento. Por eso 900 ms pasa a ser el **techo** y no el plazo:

    "como un seis"        -> respuesta completa   -> cierra a 450 ms
    "el dolor esta..."    -> fragmento a medias   -> espera el techo de 900 ms

La maquinaria para saberlo ya existe y no cuesta nada nuevo. A los 350 ms de pausa el
servidor ya arranco la transcripcion especulativa (`stt/sesion.py`), asi que cuando
se cumplen los 450 ms suele haber texto sobre el que decidir. Si no lo hay, se espera
el techo: la duda siempre se resuelve escuchando mas, nunca menos.

**Que cuenta como completa.** Solo una respuesta que resuelve por si sola el dominio
que se pregunto: un numero para el dolor, una temperatura o una negacion para la
fiebre, una categoria clara para la herida. Nada de inferencias. Y tres guardas que
mandan sobre todo lo anterior, porque el coste de equivocarse es cortarle la frase al
paciente:

  1. **Conector colgante.** "como un seis pero" no esta completa aunque tenga el seis.
     El paciente esta a punto de decir lo mas importante -- lo que va despues del
     "pero" suele ser el matiz clinico.
  2. **El turno no es una respuesta.** Quien pregunta, quien pide un humano, quien
     intenta manipular al agente o quien habla en nombre de otro dice frases largas y
     las dice enteras. Eso lo decide `guardrails.clasificar`, que ya existe y ya es
     la unica implementacion de "que clase de turno es este". Escribir aqui una
     segunda -- un puñado de regex para detectar preguntas -- habria sido el error de
     tener dos verdades: la primera version de este modulo daba "no puedo caminar"
     por pregunta, porque contenia la palabra "puedo".
  3. **Audio degradado.** Si el canal esta sucio, no se acorta nada.

Este modulo es puro y no toca el reloj: el tiempo lo aporta quien llama. Asi
`eval/escucha.py` puede pasarle las 18 grabaciones humanas trama a trama y reportar a
que milisegundo habria cerrado cada una, contra los 900 ms de referencia.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..clinical.normalizer import normalizar_turno, sin_tildes
from .guardrails import Intencion, clasificar

# Plazo minimo. Por debajo de esto no se cierra ni con la respuesta mas clara del
# mundo: son ~150 ms mas que la pausa que ya dispara la transcripcion especulativa,
# el tiempo justo para que haya texto que juzgar.
MS_CIERRE_MINIMO = 450.0

# Techo. Es el plazo que rige hoy y sigue rigiendo cuando la respuesta no esta clara.
MS_CIERRE_MAXIMO = 900.0

# Palabras con las que nadie termina una frase. Si el turno acaba en una de estas, lo
# que viene despues es justo lo que hay que oir.
CONECTORES_COLGANTES = frozenset(
    {
        "y", "e", "o", "u", "pero", "mas", "aunque", "porque", "pues", "que",
        "como", "cuando", "donde", "si", "aun", "ni", "tambien", "tampoco",
        "entonces", "asi", "osea", "sea", "este", "eh", "em", "mmm", "digamos",
        "sino", "salvo", "excepto", "desde", "hasta", "para", "por", "con",
        "sin", "en", "de", "del", "al", "a", "el", "la", "los", "las", "un",
        "una", "unos", "unas", "mi", "su", "le", "me", "se",
    }
)

# "o sea" y "es que" se escriben separadas y quedan colgando igual.
COLAS_COLGANTES = ("o sea", "es que", "lo que", "es decir", "por lo")

# Categorias de pista que resuelven su dominio por si solas.
DOMINIOS_POR_PISTA = ("herida", "movilidad", "apetito", "sueno")


@dataclass
class Completitud:
    """El veredicto, con el motivo. El motivo va al log y al arnes, no a la pantalla."""

    completa: bool
    motivo: str

    def __bool__(self) -> bool:
        return self.completa


def _cola(base: str) -> str:
    """Ultima palabra util del turno, sin puntuacion."""

    limpio = re.sub(r"[^\w\s]", " ", base).strip()
    partes = limpio.split()
    return partes[-1] if partes else ""


def _cuelga(base: str) -> bool:
    """El turno acaba en algo que anuncia que viene mas."""

    plano = re.sub(r"[^\w\s]", " ", base)
    plano = re.sub(r"\s+", " ", plano).strip()
    termina_en_cola = any(plano.endswith(c) for c in COLAS_COLGANTES)
    return _cola(base) in CONECTORES_COLGANTES or termina_en_cola


def _resuelve_el_dominio(norm, dominio: str) -> bool:
    """El turno contesta, por si solo, lo que se pregunto.

    Deliberadamente estrecho. Cada rama es un dato que el paciente dijo explicito;
    ninguna es una inferencia. Cerrar el turno antes de tiempo por una inferencia
    seria pagar en informacion clinica un ahorro de 450 ms.
    """

    numeros = norm.numeros

    if dominio == "dolor":
        resuelve = numeros.dolor_nrs is not None
    elif dominio == "fiebre":
        # Una temperatura SIN decimal todavia puede estar creciendo, y por eso no cierra.
        #
        # En español la decima se dice despues del entero -- "treinta y ocho **dos**",
        # "treinta y siete **cinco**", "treinta y ocho **y medio**" -- asi que "treinta y
        # ocho" es un prefijo tan valido como un valor final. `eval/cortes_falsos.py` lo
        # encontro recorriendo los prefijos de las 18 grabaciones, y lo que costaba era
        # clinico, no de ritmo:
        #
        #   "Treinta y siete cinco"  ->  cerraba en "Treinta y siete"  ->  37.0 y no 37.5
        #
        # 37.5 cumple la bandera amarilla de febricula (>= 37.4) y 37.0 no cumple nada:
        # el cierre anticipado **borraba la bandera**. Un falso negativo clinico para
        # ahorrar 450 ms es el peor cambio posible en este sistema.
        #
        # El precio, dicho: un paciente que de verdad tiene 38 justos espera el techo. No
        # se pierde el dato, se tarda 450 ms mas -- que es el lado correcto de la duda, y
        # es la regla que este modulo ya declaraba: se resuelve escuchando mas, nunca
        # menos.
        temperatura_firme = (
            numeros.temperatura_c is not None
            and not numeros.temperatura_fuera_de_dominio
            and numeros.temperatura_c % 1 != 0
        )
        resuelve = temperatura_firme or numeros.fiebre_negada or numeros.sin_termometro
    elif dominio in DOMINIOS_POR_PISTA:
        resuelve = norm.pistas.get(dominio) is not None
    else:
        resuelve = False

    return resuelve


def respuesta_completa(
    texto: str, dominio: str, ms_silencio: float, ms_minimo: float | None = None
) -> Completitud:
    """Decide si el turno del paciente se puede cerrar ya.

    `dominio` es el que el agente acaba de preguntar. Vacio -- el paciente habla sin
    que se le haya preguntado nada -- nunca cierra antes de tiempo: no hay nada
    contra lo que medir si la respuesta esta completa.

    `ms_minimo` lo pasa el servidor desde `config.cierre_min_ms`, para que la variable
    de entorno sirva de verdad para algo. Sin el, rige la constante de este modulo.
    """

    piso = MS_CIERRE_MINIMO if ms_minimo is None else ms_minimo
    base = sin_tildes((texto or "").strip().lower())
    veredicto = Completitud(False, "")

    if ms_silencio < piso:
        veredicto = Completitud(False, f"pausa de {ms_silencio:.0f} ms, aun corta")
    elif not base:
        veredicto = Completitud(False, "todavia no hay texto que juzgar")
    elif not dominio:
        veredicto = Completitud(False, "no hay pregunta abierta contra la que medir")
    elif _cuelga(base):
        veredicto = Completitud(False, f"termina en '{_cola(base)}', viene mas")
    else:
        norm = normalizar_turno(texto, dominio)
        cls = clasificar(
            texto,
            habla_tercero=norm.registro.habla_tercero,
            audio_degradado=norm.canal.degradado,
        )
        if cls.intencion is not Intencion.RESPUESTA:
            veredicto = Completitud(False, f"el turno es {cls.intencion.value}")
        elif _resuelve_el_dominio(norm, dominio):
            veredicto = Completitud(True, f"{dominio} resuelto en el propio turno")
        else:
            veredicto = Completitud(False, f"{dominio} sin resolver")

    return veredicto
