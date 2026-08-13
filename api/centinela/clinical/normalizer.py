"""Normalizacion del habla del paciente colombiano.

El reto lo dice explicitamente: el paciente "no tiene conocimiento medico --a
veces ni un termometro-- y describe lo que siente en lenguaje cotidiano,
ambiguo y regional". El ejemplo que dan es *"Me duele como aqui abajito de la
axila hace como 20 minutos"*.

Este modulo hace tres cosas antes de que el modelo vea el texto:

1. **Limpia el ruido de canal** y lo reporta. Los marcadores de la capa 2 del
   dataset (`[inaudible]`, `[silencio]`, `...`) no son texto: son senal sobre la
   calidad del audio. Si un turno viene muy degradado, el agente debe repetir la
   pregunta, no adivinar la respuesta.

2. **Extrae los numeros con reglas, no con el modelo.** Un dolor "como un 6" y
   una temperatura de "37 y pico" son extraibles con precision casi perfecta por
   expresion regular. Pedirle eso a un modelo de 3.8B introduce un riesgo
   innecesario en el dato que mas pesa en la decision clinica, y gasta latencia.
   El modelo se reserva para lo cualitativo, donde si aporta.

3. **Detecta el registro del hablante.** El dataset trae 928 turnos de estilo
   `minimizador_sintomas` y 733 `evasivo`. Un paciente que dice "apenas un
   poquito de enrojecimiento, nada que me preocupe" esta reportando el mismo
   hallazgo clinico que otro que dice "la herida esta muy roja", y el motor de
   decision necesita saber que el reporte viene atenuado para indagar mas.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .thresholds import ESTILOS_QUE_MINIMIZAN  # noqa: F401  (documenta el vinculo)


# --------------------------------------------------------------------------
# Ruido de canal
# --------------------------------------------------------------------------

MARCADORES_RUIDO = (
    ("[inaudible]", "inaudible"),
    ("[silencio]", "silencio"),
    ("[ruido]", "ruido"),
    ("[interrupcion]", "interrupcion"),
)

RE_PUNTOS_SUSPENSIVOS = re.compile(r"\s*\.{3,}\s*")
RE_ESPACIOS = re.compile(r"\s+")
RE_TRUNCADO = re.compile(r"\b(\w{2,})-(?=\s|$)")  # "fu-" en "el dolor si esta fu-"


@dataclass
class SenalCanal:
    inaudibles: int = 0
    silencios: int = 0
    truncamientos: int = 0
    caracteres_utiles: int = 0
    # Cuantos caracteres tenia el turno antes de quitarle los marcadores de ruido.
    # Es lo que permite distinguir "corto porque el paciente fue breve" de "corto
    # porque el ruido se comio la frase".
    caracteres_originales: int = 0

    @property
    def proporcion_perdida(self) -> float:
        if self.caracteres_originales <= 0:
            valor = 0.0
        else:
            perdidos = max(0, self.caracteres_originales - self.caracteres_utiles)
            valor = perdidos / self.caracteres_originales
        return valor

    @property
    def degradado(self) -> bool:
        """El turno esta demasiado danado para interpretarlo con seguridad.

        La version anterior de esta propiedad tenia un fallo que rompia la
        conversacion entera: consideraba degradado todo turno con menos de 12
        caracteres utiles. En un cuestionario clinico, las respuestas mas
        frecuentes son *"No"*, *"Un seis"*, *"Normal"*, *"Si senora"* -- todas por
        debajo de ese umbral. El agente marcaba cada una como audio degradado,
        respondia "perdone, no le escuche bien", y no avanzaba nunca: bucle
        infinito en el primer dominio.

        Lo corto no es lo mismo que lo danado. Ahora la decision se toma sobre las
        senales de dano de verdad -- marcadores de inaudible, silencio, y cuanto
        texto se perdio al limpiarlos -- y no sobre la longitud de la respuesta.
        """

        if not self.caracteres_utiles:
            # No quedo nada que interpretar. Da igual si lo que habia eran
            # marcadores de inaudible o solo puntos suspensivos: en los dos casos
            # el turno esta vacio y hay que pedir que repita.
            malo = True
        elif self.inaudibles >= 2 and self.proporcion_perdida > 0.35:
            # Varios tramos inaudibles y se perdio buena parte del turno.
            malo = True
        elif self.inaudibles >= 4:
            # Muchos cortes, aunque quede texto: no es fiable.
            malo = True
        else:
            malo = False
        return malo


def limpiar_ruido(texto: str) -> tuple[str, SenalCanal]:
    senal = SenalCanal(caracteres_originales=len(texto.strip()))
    limpio = texto

    for marcador, clase in MARCADORES_RUIDO:
        n = limpio.lower().count(marcador)
        if n:
            if clase == "inaudible":
                senal.inaudibles += n
            elif clase == "silencio":
                senal.silencios += n
            limpio = re.sub(re.escape(marcador), " ", limpio, flags=re.IGNORECASE)

    senal.truncamientos = len(RE_TRUNCADO.findall(limpio))
    limpio = RE_PUNTOS_SUSPENSIVOS.sub(" ", limpio)
    limpio = RE_ESPACIOS.sub(" ", limpio).strip()
    senal.caracteres_utiles = len(limpio)
    return limpio, senal


# --------------------------------------------------------------------------
# Registro del hablante
# --------------------------------------------------------------------------

ATENUADORES = (
    "apenas", "poquito", "poquita", "un poco", "casi nada", "nada que", "leve",
    "levecito", "no es nada", "normalito", "ahi mas o menos", "casi no",
    "nada de que preocuparse", "no me preocupa", "no tanto", "medio",
    "por ahi", "creo que no", "casi bien",
)

AMPLIFICADORES = (
    "muchisimo", "horrible", "no aguanto", "insoportable", "terrible",
    "durisimo", "malisimo", "peor", "no puedo mas", "espantoso", "fatal",
)

EVASIVOS = (
    "mejor hablemos", "cambiando de tema", "eso no importa", "no le se decir",
    "prefiero no", "siga con la otra", "no se", "no me acuerdo", "quien sabe",
)

ANSIOSOS = (
    "digame que", "por favor digame", "estoy muy preocupad", "me tiene preocupad",
    "necesito esa tranquilidad", "ay dios", "sera que", "me da miedo",
    "confirmeme", "es grave",
)

CONFUNDIDOS = (
    "como dijo", "que dia", "no me acuerdo si", "espere", "ah?", "perdon, que",
    "me pregunto de", "ya ni se",
)

TERCERO = (
    "soy la hija", "soy el hijo", "soy el cuidador", "soy la cuidadora",
    "habla la esposa", "habla el esposo", "soy la senora de", "yo le cuento",
    "el no escucha", "ella no escucha", "esta descansando",
)


@dataclass
class RegistroHabla:
    atenua: bool = False
    amplifica: bool = False
    evade: bool = False
    ansioso: bool = False
    confundido: bool = False
    habla_tercero: bool = False
    marcas: list[str] = field(default_factory=list)

    @property
    def estilo_inferido(self) -> str:
        if self.habla_tercero:
            estilo = "tercero"
        elif self.evade:
            estilo = "evasivo"
        elif self.atenua:
            estilo = "minimizador_sintomas"
        elif self.ansioso:
            estilo = "ansioso"
        elif self.confundido:
            estilo = "confundido"
        else:
            estilo = "colaborativo"
        return estilo


def sin_tildes(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", texto.lower())
        if unicodedata.category(c) != "Mn"
    )


def detectar_registro(texto: str) -> RegistroHabla:
    base = sin_tildes(texto)
    r = RegistroHabla()

    for termino in TERCERO:
        if termino in base:
            r.habla_tercero = True
            r.marcas.append(f"tercero:{termino}")

    for termino in ATENUADORES:
        if termino in base:
            r.atenua = True
            r.marcas.append(f"atenua:{termino}")

    for termino in AMPLIFICADORES:
        if termino in base:
            r.amplifica = True
            r.marcas.append(f"amplifica:{termino}")

    for termino in EVASIVOS:
        if termino in base:
            r.evade = True
            r.marcas.append(f"evade:{termino}")

    for termino in ANSIOSOS:
        if termino in base:
            r.ansioso = True
            r.marcas.append(f"ansioso:{termino}")

    for termino in CONFUNDIDOS:
        if termino in base:
            r.confundido = True
            r.marcas.append(f"confundido:{termino}")

    return r


# --------------------------------------------------------------------------
# Numeros clinicos por regla
# --------------------------------------------------------------------------

PALABRA_A_NUMERO = {
    "cero": 0, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4,
    "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10,
}
# "un" NO esta en el mapa a proposito. Es el articulo indefinido antes que el
# numero, y meterlo hacia que "como en un tres" se leyera como dolor 1: la regex
# capturaba el articulo y se detenia antes de llegar al numero de verdad. Un
# paciente que quiere decir el numero uno dice "uno".

# Decenas y treintas, para las temperaturas dichas en palabras. Whisper escribe
# los numeros en letra a menudo, y mucho mas en turnos cortos: "treinta y siete
# cinco" es exactamente como un paciente colombiano dice 37.5.
DECENAS = {"treinta": 30, "cuarenta": 40}

_DIGITOS = "cero|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve"

# Las tres formas de decir el decimal, en un solo patron y con grupos nombrados porque
# ya no caben en la cabeza. La tercera -- **"y medio"** -- faltaba, y no era un detalle
# de estilo: es probablemente la forma mas comun de decir una temperatura en espanol, y
# sin ella "treinta y siete y medio" se leia 37.0 en vez de 37.5. Eso cruza el umbral de
# febricula (`FIEBRE_AMARILLO_C = 37.4`) en la direccion mala: el paciente reportaba
# febricula y el sistema anotaba temperatura normal. Un falso negativo clinico que los
# 160 casos oficiales no podian ver, porque vienen con la cifra ya escrita.
RE_TEMP_PALABRAS = re.compile(
    r"\b(?P<decena>treinta|cuarenta)"
    r"(?:\s+y\s+(?P<unidad>uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve))?"
    r"(?:"
    rf"\s+(?:punto|coma|con)\s+(?P<dec_marcado>{_DIGITOS})"   # "punto cinco", "con cinco"
    # "y medio" y "y pico" valen los dos +0.5. Lo de "y pico" ya estaba resuelto para
    # la cifra ("37 y pico") con el mismo criterio -- es el punto medio del intervalo
    # que el paciente describe -- pero no para la letra, que es como se dice hablando.
    r"|\s+y\s+(?P<dec_medio>medio|pico)"
    rf"|\s+(?P<dec_suelto>{_DIGITOS})"                         # "treinta y siete cinco"
    r")?"
)

# "un 6", "como un 6", "en 5", "6 de 10", "6/10", "seis"
RE_DOLOR_NUM = re.compile(
    r"(?:(?:un|en|como un|como en|de|nivel|escala de)\s+)?"
    r"\b(10|[0-9])\b(?:\s*(?:/|de|sobre)\s*10)?"
)
RE_DOLOR_PALABRA = re.compile(
    r"\b(?:un|en|como un|como en|de)\s+(" + "|".join(PALABRA_A_NUMERO) + r")\b"
)
# Un numero solo, con las muletillas que lo suelen acompanar: "Seis", "Un seis.",
# "Como un cuatro", "Pues como en un tres", "Yo diria unos cinco".
# Solo se usa cuando se sabe que dominio se pregunto -- ver `extraer_numeros`.
#
# Las muletillas se permiten en cualquier combinacion y numero porque nadie
# responde con el numero pelado: siempre viene envuelto en dos o tres palabras de
# relleno, y exigir una forma concreta era lo que hacia fallar "como en un tres".
_MULETILLAS_NUMERO = (
    r"(?:pues|bueno|este|yo\s+diria|diria|creo\s+que|seria|es|esta|"
    r"como|en|un|unos|de|el|mas\s+o\s+menos|ahi|por\s+ahi)"
)
RE_NUMERO_SUELTO = re.compile(
    r"^\W*(?:" + _MULETILLAS_NUMERO + r"\s+){0,4}"
    r"(10|[0-9]|" + "|".join(PALABRA_A_NUMERO) + r")\b"
    r"(?:\s*(?:/|de|sobre)\s*(?:10|diez))?"
    r"(?:\s+(?:mas\s+o\s+menos|creo|diria|por\s+ahi|de\s+diez))?\W*$"
)

# "37.5", "37,5", "38 grados", "37 y pico", "treinta y ocho"
RE_TEMP_DECIMAL = re.compile(r"\b(3[5-9]|4[0-2])[.,]([0-9])\b")
RE_TEMP_ENTERA = re.compile(r"\b(3[5-9]|4[0-2])\b(?!\s*[.,]?\s*\d)")
RE_TEMP_Y_PICO = re.compile(r"\b(3[5-9])\s*(?:grados?\s*)?y\s*pico\b")

# La temperatura deletreada digito a digito. No es una forma de hablar: es lo que
# **Whisper escribe** cuando la oye. Medido en una llamada por voz real, el paciente dijo
# "treinta y siete cinco" y la transcripcion fue "Tres, siete, cinco." -- que el
# normalizador no reconocia, asi que el turno no producia temperatura.
#
# Solo se aplica con contexto de temperatura o cuando el agente acaba de preguntar por
# la fiebre. Sin esa guarda, tres digitos sueltos son demasiadas otras cosas.
RE_TEMP_DELETREADA = re.compile(
    r"\btres[\s,]+(cinco|seis|siete|ocho|nueve)[\s,]+"
    r"(cero|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve)\b"
)

# Magnitudes que se dicen con las mismas cifras que una temperatura. Es la unica
# fuente real de confusion cuando el numero se acepta sin contexto: "tengo 38
# anos", "hace 40 minutos", "peso 40 kilos". La unidad va siempre detras del
# numero, asi que mirar lo que sigue al fragmento resuelve el caso completo.
RE_UNIDAD_NO_TEMPERATURA = re.compile(
    r"^\W*(?:anos?|meses|mes|semanas?|dias?|horas?|minutos?|min|segundos?|"
    r"veces|vez|kilos?|kg|libras?|gramos?|gr|mg|miligramos?|mililitros?|ml|"
    r"pastillas?|tabletas?|gotas?|cucharadas?|cuadras?|metros?|cm|centimetros?|"
    r"mil|millones?|personas?|puntos?|por\s+ciento)\b"
)

CONTEXTO_TEMPERATURA = (
    "grado", "temperatura", "termometro", "fiebre", "calentura", "febril",
    "afiebrad", "calor", "escalofri",
)

CONTEXTO_DOLOR = ("dolor", "duele", "molestia", "escala", "punzada", "puntada", "adolorid")

SINONIMOS_FIEBRE_SUBJETIVA = (
    "calentura", "afiebrad", "me senti caliente", "cuerpo caliente", "calorcito",
    "escalofrio", "escalofrios", "destemplad", "me hervia", "acalorad", "febril",
    "como con fiebre",
)

SIN_TERMOMETRO = (
    "no tengo termometro", "no me la he tomado", "no me he medido", "no tengo con que",
    "no me la tome", "no tengo termometro en casa", "sin termometro",
    # Whisper confunde "la" con "lo" en esta frase con frecuencia; medido en
    # eval/escucha.py. La variante cuesta una linea y salva el turno.
    "no me lo he tomado", "no me lo tome", "no me he tomado la temperatura",
)

# El paciente NIEGA la fiebre. Es una respuesta, no un dato ausente.
#
# Sin esto, "no he tenido fiebre" no producia nada: la fiebre es un dominio
# numerico y no hay numero que extraer, asi que el dominio quedaba sin resolver,
# el agente repreguntaba, y al cerrar escalaba a amarillo por dominio sin indagar.
# Consecuencia medida: un paciente que contesta todo con normalidad cerraba en
# AMARILLO con cero banderas. Justo el escenario "claramente no hay que escalar"
# que la rubrica dice que se prueba.
#
# Lo que NO se hace es inventar una temperatura. Negar la fiebre resuelve la
# pregunta, no produce una medicion: el resumen sigue diciendo que no hay valor
# objetivo, y lo dice con su motivo.
RE_NIEGA_FIEBRE = re.compile(
    r"\b(?:no|ninguna|ningun|nada\s+de|sin)\b[^.;,]{0,20}\b(?:fiebre|calentura|febril)\b"
)


@dataclass
class NumerosClinicos:
    dolor_nrs: int | None = None
    temperatura_c: float | None = None
    fiebre_subjetiva: bool = False
    sin_termometro: bool = False
    fiebre_negada: bool = False
    evidencia_dolor: str = ""
    evidencia_temperatura: str = ""
    # La temperatura llego sin que el turno hablara de fiebre y sin que fuera el
    # dominio preguntado. Se acepta igual (ver `extraer_numeros`), pero queda
    # marcado: es informacion util en la traza y lo que un test puede afirmar.
    temperatura_fuera_de_dominio: bool = False


def _mide_otra_cosa(base: str, fin: int) -> bool:
    """True si detras del numero viene una unidad que no es una temperatura.

    Es la unica guarda que hace falta para aceptar un numero con forma de
    temperatura sin contexto ninguno: la unidad va siempre despues de la cifra.
    """

    return bool(RE_UNIDAD_NO_TEMPERATURA.match(base[fin:fin + 24]))


def _primera_valida(patron: re.Pattern[str], base: str) -> re.Match[str] | None:
    """La primera coincidencia que NO esta midiendo otra cosa.

    Con `search` a secas se perdia el dato en un turno con dos cifras: "tengo 38
    anos y me marco 39" descartaba el 38 por la unidad y no seguia buscando, asi
    que la temperatura de verdad se caia. Hay que recorrer las candidatas.
    """

    valida: re.Match[str] | None = None
    for m in patron.finditer(base):
        if valida is None and not _mide_otra_cosa(base, m.end()):
            valida = m
    return valida


def temperatura_en_palabras(base: str) -> tuple[float, str] | None:
    """Interpreta una temperatura dicha en letra.

    Whisper escribe los numeros en palabras con frecuencia, y casi siempre en los
    turnos cortos. Un paciente colombiano dice la temperatura asi:

        "treinta y siete cinco"        -> 37.5
        "treinta y ocho"               -> 38.0
        "treinta y siete punto cinco"  -> 37.5

    Sin esto, el turno "me tome la temperatura y estaba en treinta y siete cinco"
    no producia ningun dato y el agente volvia a preguntar lo mismo.
    """

    resultado: tuple[float, str] | None = None
    m = _primera_valida(RE_TEMP_PALABRAS, base)

    if m is not None:
        valor = float(DECENAS[m.group("decena")])
        if m.group("unidad"):
            valor += PALABRA_A_NUMERO[m.group("unidad")]

        decimal = m.group("dec_marcado") or m.group("dec_suelto")
        if m.group("dec_medio"):
            valor += 0.5
        elif decimal:
            valor += PALABRA_A_NUMERO[decimal] / 10

        if 34.0 <= valor <= 43.0:
            resultado = (valor, m.group(0))

    return resultado


def temperatura_deletreada(base: str) -> tuple[float, str] | None:
    """"Tres, siete, cinco." -> 37.5. Lo que Whisper escribe, no lo que se dice."""

    resultado = None
    m = RE_TEMP_DELETREADA.search(base)

    if m is not None:
        valor = 30.0 + PALABRA_A_NUMERO[m.group(1)] + PALABRA_A_NUMERO[m.group(2)] / 10
        if 34.0 <= valor <= 43.0:
            resultado = (valor, m.group(0))

    return resultado


def buscar_temperatura(base: str, con_contexto: bool = False) -> tuple[float, str] | None:
    """La temperatura del turno, dicha en cifra o en letra.

    Las cuatro formas van en orden de especificidad. El entero pelado va ultimo a
    proposito: es la forma que mas se parece a otra magnitud, y por eso es la que
    necesita la guarda de unidad.
    """

    encontrada: tuple[float, str] | None = None

    m = _primera_valida(RE_TEMP_DECIMAL, base)
    if m is not None:
        encontrada = (float(f"{m.group(1)}.{m.group(2)}"), m.group(0))
    else:
        en_palabras = temperatura_en_palabras(base)
        if en_palabras is not None:
            encontrada = en_palabras
        else:
            m = RE_TEMP_Y_PICO.search(base)
            if m is not None:
                # "37 y pico" se interpreta como 37.5: es el punto medio del
                # intervalo que el paciente esta describiendo.
                encontrada = (float(m.group(1)) + 0.5, m.group(0))
            else:
                m = _primera_valida(RE_TEMP_ENTERA, base)
                if m is not None:
                    encontrada = (float(m.group(1)), m.group(0))
                elif con_contexto:
                    # La ultima y la menos especifica: tres digitos deletreados. Va al
                    # final y solo con contexto, porque es la que mas se parece a otra
                    # cosa dicha con las mismas palabras.
                    encontrada = temperatura_deletreada(base)

    return encontrada


def extraer_numeros(texto: str, dominio_objetivo: str = "") -> NumerosClinicos:
    """Extraccion numerica por reglas, sensible al contexto.

    Dos fuentes de contexto, y la segunda tardo en aparecer:

    **El texto del turno.** En "me operaron el 7 y el dolor es un 3" hay dos
    numeros y solo uno es el dolor, asi que el numero se busca en la vecindad de
    una palabra del dominio.

    **La pregunta que se acaba de hacer.** Esta faltaba, y su ausencia rompia la
    conversacion en el caso mas comun de todos. Si el agente pregunta *"en que
    numero pondria el dolor, de cero a diez?"* y el paciente responde *"Un seis"*,
    el turno no contiene ninguna palabra del dominio -- el contexto esta en la
    pregunta, no en la respuesta. El sistema no extraia nada, el dominio se
    quedaba sin resolver, y el agente volvia a preguntar lo mismo indefinidamente.

    Un numero suelto solo se acepta cuando se sabe que dominio se pregunto: sin
    ese dato, "seis" en mitad de una frase puede ser cualquier cosa.
    """

    base = sin_tildes(texto)
    n = NumerosClinicos()

    # --- temperatura ---
    #
    # Tres fuentes de contexto, en orden de confianza. Las dos primeras estaban
    # desde el principio; la tercera se anadio despues de verla fallar en una
    # llamada de verdad.
    #
    #   1. El turno habla de temperatura: "38 de fiebre", "me dio calentura".
    #   2. El agente acaba de preguntar por la fiebre.
    #   3. Ninguna de las dos, pero el numero tiene forma de temperatura.
    #
    # La tercera existe porque dentro de este cuestionario un numero entre 35 y 42
    # no puede ser otra cosa: la escala de dolor va de 0 a 10 y ningun otro dominio
    # produce cifras. El paciente que responde "treinta y ocho" mientras el agente
    # todavia pregunta por el sueno esta dando su temperatura, y descartarla era
    # perder el dato que mas pesa en la decision -- justo el falso negativo que la
    # rubrica llama catastrofico.
    #
    # `_mide_otra_cosa` cubre el unico falso positivo posible, que es la magnitud
    # dicha con la misma cifra: "tengo 38 anos", "hace 40 minutos", "peso 40 kilos".
    hay_contexto_temp = any(t in base for t in CONTEXTO_TEMPERATURA)
    pregunta_por_fiebre = dominio_objetivo == "fiebre"

    encontrada = buscar_temperatura(base, con_contexto=hay_contexto_temp or pregunta_por_fiebre)
    if encontrada is not None:
        n.temperatura_c, n.evidencia_temperatura = encontrada
        n.temperatura_fuera_de_dominio = not (hay_contexto_temp or pregunta_por_fiebre)

    n.fiebre_subjetiva = any(t in base for t in SINONIMOS_FIEBRE_SUBJETIVA)
    n.sin_termometro = any(t in base for t in SIN_TERMOMETRO)

    # La negacion se evalua DESPUES de la sensacion subjetiva y la desactiva.
    # "No me senti caliente" contiene "me senti caliente", asi que sin este paso
    # una negacion se leia como fiebre subjetiva PRESENTE -- exactamente al revés
    # de lo que dijo el paciente.
    #
    # Y no cuenta como negacion si lo que el paciente esta diciendo es que no se
    # la midio: "no me he tomado la temperatura" no es "no tengo fiebre".
    if not n.sin_termometro and RE_NIEGA_FIEBRE.search(base):
        n.fiebre_negada = True
        n.fiebre_subjetiva = False

    # --- dolor ---
    hay_contexto_dolor = any(t in base for t in CONTEXTO_DOLOR)
    pregunta_por_dolor = dominio_objetivo == "dolor"

    if hay_contexto_dolor or pregunta_por_dolor:
        # Se excluye el fragmento que ya se interpreto como temperatura para no
        # leer "37" como un dolor de 7.
        limpio = base
        if n.evidencia_temperatura:
            limpio = limpio.replace(n.evidencia_temperatura, " ")
        limpio = re.sub(r"\b(3[5-9]|4[0-2])\b", " ", limpio)

        # Si se pregunto por el dolor y el turno es solo un numero, ese numero es
        # la respuesta. Se comprueba primero porque es el caso mas claro.
        if pregunta_por_dolor:
            m = RE_NUMERO_SUELTO.match(limpio.strip())
            if m:
                bruto = m.group(1)
                valor = int(bruto) if bruto.isdigit() else PALABRA_A_NUMERO[bruto]
                if 0 <= valor <= 10:
                    n.dolor_nrs = valor
                    n.evidencia_dolor = texto.strip()

        if n.dolor_nrs is None:
            m = RE_DOLOR_NUM.search(limpio)
            if m:
                valor = int(m.group(1))
                if 0 <= valor <= 10:
                    n.dolor_nrs = valor
                    n.evidencia_dolor = m.group(0).strip()

        if n.dolor_nrs is None:
            m = RE_DOLOR_PALABRA.search(limpio)
            if m:
                n.dolor_nrs = PALABRA_A_NUMERO[m.group(1)]
                n.evidencia_dolor = m.group(0).strip()

    return n


# --------------------------------------------------------------------------
# Pistas cualitativas: acortan el trabajo del modelo y sirven de red de
# seguridad si el modelo se equivoca en un hallazgo de alarma.
# --------------------------------------------------------------------------

PISTAS_HERIDA = {
    "secrecion_purulenta": (
        "pus", "purulent", "liquido amarillo", "amarillo saliendo", "liquido verde",
        "secrecion amarilla", "material amarillo", "mal olor", "huele mal",
        "feo olor", "supura", "supurando", "sale liquido", "sale materia",
        "liquido con mal olor", "verdoso",
        # "amarillento" y "amarillenta" son como lo dice la gente, y faltaban: el lexico
        # solo tenia "liquido amarillo". Medido en una llamada real, "me esta saliendo un
        # liquido amarillento" no producia ninguna pista de herida.
        "amarillent",
    ),
    "eritema_leve": (
        "rojita", "rojito", "enrojecid", "enrojecimiento", "roja alrededor",
        "un poco roja", "colorad", "irritada", "rosadita",
        # Hinchazon e inflamacion son signos inflamatorios locales y faltaban enteras:
        # estaba solo el diminutivo "inflamadita". Medido, "la herida se ve roja e
        # hinchada" y "esta hinchada alrededor" no producian ninguna pista, asi que
        # caian al modelo -- 2.2 s -- y con el modelo caido se perdian. La categoria es
        # la amarilla, que es la direccion segura: inflamacion sin secrecion no es
        # purulencia, pero tampoco es una herida normal.
        "hinchad", "hinchazon", "inflamad", "inflamacion",
        # "roja" sola no entra: aparece fuera de la herida ("la pastilla roja"). Se
        # exige el contexto que la ata a la herida.
        "se ve roja", "esta roja", "muy roja", "bien roja", "roja y", "roja e",
    ),
    "normal": (
        "se ve bien", "esta bien", "normal", "limpiecita", "cerrando bien",
        "sin nada raro", "no le veo nada", "cicatrizando bien",
    ),
}

# --------------------------------------------------------------------------
# Menciones: ¿el paciente HABLO de este dominio, sea para bien o para mal?
#
# Es otra pregunta que las pistas de arriba, y por eso es otra tabla. `PISTAS_*` clasifica
# la GRAVEDAD ("¿esta purulenta, roja o normal?"); esto solo detecta el TEMA ("¿dijo algo
# de la herida?"). Una frase puede mencionar la herida sin decir nada clasificable --"le
# tuve que cambiar la gasa tres veces"-- y por eso las dos tablas no se pueden fusionar.
#
# Existe por una alerta roja fabricada en una llamada real. El STT se comio un "no" y dejo
# "cuando me tomo las pasillas ya me hace el dolor"; el modelo devolvio
# `herida = secrecion_purulenta`; el motor escalo a rojo, correctamente, sobre un dato
# inventado. El agente le leyo de vuelta "me dice liquido amarillo o pus saliendo de la
# herida" y el paciente lo nego dos veces.
#
# Lo que estas listas sostienen: un hallazgo del valor MAS GRAVE de su escala, en un dominio
# que nadie pregunto y que el paciente no menciono ni de pasada, no se admite como alarma.
# Se anota en la traza y se le pregunta por ese dominio en su turno del guion -- que llega
# igual, porque la conversacion la conduce una maquina de estados.
#
# Los terminos son de anatomia y de cuidado, no de gravedad, a proposito: son los que
# aparecen cuando alguien habla del tema sin usar vocabulario clinico. Las cuatro parafrasis
# rojas de `eval/redteam.py` entran todas por aqui -- "cortaron", "cortada", "herida",
# "gasa" -- y la frase de las pastillas no entra por ninguno.
# --------------------------------------------------------------------------

MENCIONES = {
    "herida": (
        "herida", "cortada", "cortaron", "cortadura", "cicatriz", "cicatriz",
        "punto", "puntos", "sutura", "incision", "gasa", "venda", "vendaje",
        "aposito", "curacion", "curaciones", "costra", "grapa", "grapas",
        "me operaron", "donde me abrieron", "la operacion se",
    ),
    "movilidad": (
        "caminar", "camino", "camina", "andar", "ando", "pararme", "parar",
        "levantarme", "levantar", "moverme", "mover", "movilidad", "pierna",
        "piernas", "pie", "pies", "rodilla", "cadera", "apoyar", "apoyo",
        "muleta", "muletas", "caminador", "baston", "arrastrar", "arrastrarme",
        "sostener", "sostenerme", "sentarme", "acostarme", "escalera",
    ),
    "apetito": (
        "comer", "como", "comida", "apetito", "hambre", "tragar", "alimento",
        "desayun", "almuerzo", "almorzar", "cenar", "cena", "probar bocado",
        "ganas de comer", "nausea", "vomit", "asco",
    ),
    "sueno": (
        "dormir", "duermo", "duerme", "sueno", "noche", "noches", "despierto",
        "despierta", "desvel", "descansar", "descanso", "insomnio", "acostar",
        "madrugada", "pesadilla",
    ),
}


def menciona(texto: str, dominio: str) -> bool:
    """Si el paciente habló de ese dominio, aunque no dijera nada clasificable.

    Sin tildes y en minúsculas, como el resto del normalizador: el paciente dice «cicatriz»
    y el STT escribe «cicatríz» según el día.
    """

    terminos = MENCIONES.get(dominio)
    if terminos:
        plano = sin_tildes((texto or "").lower())
        hallado = any(t in plano for t in terminos)
    else:
        # Un dominio sin lista no se puede negar: dolor y fiebre son numericos y su
        # respaldo lo da la regex que leyo la cifra, no una palabra clave.
        hallado = True
    return hallado


# Pistas que NO valen como subcadena: son palabras cortas que viven dentro de otras.
#
# `"lento"` --pista de movilidad limitada-- engancha dentro de "amari**llento**", asi que
# *"me esta saliendo un liquido amarillento"* se clasificaba como `movilidad:
# limitada_esperada` en vez de `herida: secrecion_purulenta`. Un hallazgo **rojo** convertido
# en uno amarillo, y de otro dominio. Encontrado en una llamada real: el agente respondia
# "corrijo entonces, me queda que se mueve con algo de dificultad", nunca registraba la
# herida, y repreguntaba lo mismo en bucle porque el dominio seguia sin resolver.
#
# El resto de las pistas siguen siendo subcadenas **a proposito**: "enrojecid" tiene que coger
# "enrojecida" y "enrojecido", "purulent" las dos formas, y "supura" coge "supurando". Por eso
# esto es una lista explicita y no una regla general: lo que hace falta es frontera de palabra
# donde la pista ES una palabra, sin perder los prefijos deliberados.
PISTAS_CON_FRONTERA = frozenset({"lento", "lenta", "lentos", "lentas"})

# Y estas la necesitan **solo por la izquierda**, porque su terminacion si es productiva.
#
# `"normal"` vive dentro de "**a**normal", que significa lo contrario. Medido: *"la herida
# esta anormal"* daba `herida: normal`. Un hallazgo descrito por el paciente como anormal
# archivado como normal es el falso negativo mas literal que puede tener este sistema.
#
# Pero exigir frontera por la derecha tambien romperia "normalidad" y "normalmente", que si
# quieren decir normal y son como habla la gente. De ahi que la frontera sea de un solo lado.
PISTAS_CON_FRONTERA_IZQUIERDA = frozenset({"normal"})

_RE_FRONTERA = {
    **{
        t: re.compile(rf"(?<![a-z]){re.escape(t)}(?![a-z])")
        for t in PISTAS_CON_FRONTERA
    },
    **{
        t: re.compile(rf"(?<![a-z]){re.escape(t)}")
        for t in PISTAS_CON_FRONTERA_IZQUIERDA
    },
}


def _aparece(termino: str, base: str) -> bool:
    """La pista esta en el texto, exigiendo frontera de palabra si le hace falta."""

    patron = _RE_FRONTERA.get(termino)
    if patron is None:
        encontrado = termino in base
    else:
        encontrado = patron.search(base) is not None
    return encontrado


PISTAS_MOVILIDAD = {
    # Las cuatro ultimas formas las encontro la familia `parafraseo_rojo` de
    # `eval/redteam.py`, que describe criterios de alarma con palabras que NO estan en
    # esta lista. "Tengo que arrastrarme para llegar al bano" es incapacidad funcional
    # por cualquier lectura clinica, y el turno se quedaba en amarillo con el dominio
    # sin resolver -- el falso negativo que no se ve, porque no hay error ni excepcion:
    # solo una llamada que cierra en amarillo cuando debia cerrar en rojo.
    #
    # El paciente que no puede moverse rara vez lo dice con un verbo de negacion: lo
    # dice contando lo que TIENE que hacer para lograrlo.
    "incapacitante_nueva": (
        "no puedo caminar", "no me puedo levantar", "no puedo apoyar",
        "no aguanto el peso", "no puedo pararme", "ya no puedo mover",
        "de un dia para otro no", "no me puedo mover", "no logro pararme",
        "no me puedo sostener", "no me sostengo", "tengo que arrastrarme",
        "me arrastro", "no me responde la pierna", "la pierna no me responde",
        "gateando", "en cuatro patas",
    ),
    "limitada_esperada": (
        "despacio", "con dificultad", "me cuesta", "poco a poco", "con ayuda",
        "lento", "me demoro", "con el caminador", "apoyandome",
    ),
    "normal": (
        "camino normal", "sin problema", "me muevo bien", "normal", "sin dificultad",
        "hago todo", "subo escaleras",
    ),
}

PISTAS_APETITO = {
    "muy_disminuido": (
        "no me da hambre", "casi no como", "no he podido comer", "nada de hambre",
        "se me quito el apetito", "no me provoca", "como poquito y a veces ni eso",
        "no paso nada", "no tolero la comida", "vomito",
    ),
    "levemente_disminuido": (
        "menos que antes", "un poco menos", "no como como antes", "poquito menos",
        "no tanto como antes", "algo bajo",
    ),
    "normal": ("como normal", "como bien", "sin problema para comer", "normal", "buen apetito"),
}

PISTAS_SUENO = {
    "muy_alterado": (
        "no he podido dormir", "duermo muy mal", "no duermo", "me despierto a cada rato",
        "malisimo", "no descanso", "paso la noche en vela", "no pego el ojo",
        "me despierto varias veces",
    ),
    "levemente_alterado": (
        "me despierto una vez", "regular", "mas o menos", "algo interrumpido",
        "no tan bien", "me cuesta dormirme",
    ),
    "normal": ("duermo bien", "descanso bien", "normal", "sin problema", "de corrido"),
}


# Vocabulario que indica que el turno esta HABLANDO de un dominio.
#
# Existe por un fallo real y peligroso. Las listas de arriba incluian "normal"
# como termino suelto para la categoria normal de cuatro dominios, asi que un
# paciente que preguntaba *"el dolor es un 6, no se si eso es normal"* marcaba
# herida, movilidad, apetito y sueno como NORMALES de golpe -- sin haber dicho
# una palabra sobre ninguno de los cuatro.
#
# La consecuencia no es cosmetica: el agente creia que ya conocia esos dominios
# y no volvia a preguntar por ellos. Es exactamente el mecanismo del falso
# negativo que el motor de decision esta disenado para evitar, colandose una capa
# antes. Lo encontro una captura de la interfaz donde los cuatro dominios citaban
# el mismo turno, que solo hablaba de dolor.
#
# Ahora una pista de categoria "normal" solo se acepta si el turno menciona ese
# dominio, o si es el dominio que el agente acaba de preguntar.
DOMINIO_MENCIONADO = {
    "herida": (
        "herida", "cicatriz", "punto", "sutura", "corte", "incision", "vendaje",
        "curacion", "gasa", "aposito", "grapa",
    ),
    "movilidad": (
        "camin", "mover", "movi", "levantar", "pararme", "andar", "pie", "pierna",
        "paso", "escalera", "desplaz", "apoyar", "deambul",
    ),
    "apetito": (
        "apetito", "hambre", "comer", "comid", "comi", "aliment", "trag", "boca",
        "estomago", "desayun", "almorz", "cen", "provoca", "nause", "vomit",
    ),
    "sueno": (
        "sueno", "dorm", "duerm", "noche", "descans", "acost", "despiert",
        "insomni", "madrugada",
    ),
}


def menciona_dominio(base_sin_tildes: str, dominio: str) -> bool:
    presente = any(t in base_sin_tildes for t in DOMINIO_MENCIONADO.get(dominio, ()))
    return presente


# Terminos de la categoria "normal" que NO dicen a que dominio se refieren. Son
# los que necesitan la compuerta de contexto.
#
# La regla para entrar en esta lista: si la frase no nombra su propio dominio, es
# ambigua. "Cicatrizando bien" nombra la herida; "se ve bien" puede referirse a la
# herida, a la pierna o al color de la cara.
#
# La primera version de esta lista solo tenia cuatro terminos y dejaba fuera "se
# ve bien". Lo cazo `test_normal_ambiguo_se_acepta_si_es_el_dominio_preguntado`,
# que existe precisamente para eso: comprueba que un "se ve bien" sin contexto no
# marca la herida como normal. Ante la duda, a la lista -- marcar un dominio como
# normal sin que nadie lo haya dicho es la forma mas sigilosa de producir un falso
# negativo.
TERMINOS_AMBIGUOS = frozenset({
    "normal",
    "bien",
    "esta bien",
    "se ve bien",
    "sin nada raro",
    "no le veo nada",
    "sin problema",      # movilidad y sueno; "sin problema para comer" es otra cadena
    "sin dificultad",
    "hago todo",
    "de corrido",
})


def pistas_cualitativas(texto: str, dominio_objetivo: str = "") -> dict[str, str]:
    """Categoria sugerida por dominio segun lexico observado en el dataset.

    Dos reglas gobiernan esta funcion, y las dos vienen de la asimetria clinica.

    **De mas grave a menos grave.** Si un turno contiene a la vez "sale un liquido
    amarillo" y "se ve bien", gana el hallazgo grave. Ante dos lecturas posibles
    del mismo turno, la conservadora es la que escala.

    **Un "normal" AMBIGUO necesita contexto.** Las categorias de hallazgo
    (purulenta, eritema, incapacitante...) se aceptan siempre: son especificas y
    nombran algo que el paciente solo puede haber dicho a proposito. La categoria
    "normal" se acepta si el turno habla de ese dominio o si es el dominio que se
    acaba de preguntar. Marcar un dominio como normal sin que nadie lo haya dicho
    es la forma mas sigilosa de producir un falso negativo.

    Con un matiz que costo una resolucion perdida: la compuerta solo hace falta
    para los terminos que POR SI SOLOS no dicen de que dominio hablan -- "normal",
    "bien", "sin problema". Una frase como "como normal" o "camino normal" ya
    nombra su dominio, y exigirle contexto ademas la descartaba. El sintoma medido:
    el paciente decia "como normal" mientras el agente todavia preguntaba por la
    fiebre, y el apetito se quedaba sin resolver hasta el cierre.
    """

    base = sin_tildes(texto)
    salida: dict[str, str] = {}

    for dominio, mapa in (
        ("herida", PISTAS_HERIDA),
        ("movilidad", PISTAS_MOVILIDAD),
        ("apetito", PISTAS_APETITO),
        ("sueno", PISTAS_SUENO),
    ):
        for categoria, terminos in mapa.items():
            if dominio not in salida:
                for t in terminos:
                    if _aparece(t, base):
                        if categoria == "normal":
                            admisible = (
                                t not in TERMINOS_AMBIGUOS
                                or menciona_dominio(base, dominio)
                                or dominio == dominio_objetivo
                            )
                        else:
                            admisible = True
                        if admisible:
                            salida[dominio] = categoria
                            break

    return salida


@dataclass
class TurnoNormalizado:
    texto_original: str
    texto: str
    canal: SenalCanal
    registro: RegistroHabla
    numeros: NumerosClinicos
    pistas: dict[str, str]

    @property
    def requiere_repetir(self) -> bool:
        return self.canal.degradado


def normalizar_turno(texto: str, dominio_objetivo: str = "") -> TurnoNormalizado:
    """Analiza un turno del paciente.

    `dominio_objetivo` es el dominio que el agente acaba de preguntar. Se usa solo
    para desambiguar un "normal" sin contexto: si el agente pregunto por la herida
    y el paciente dice "se ve bien", eso es la herida. Sin ese dato, un "bien"
    suelto no se atribuye a nada.
    """

    limpio, canal = limpiar_ruido(texto)
    normalizado = TurnoNormalizado(
        texto_original=texto,
        texto=limpio,
        canal=canal,
        registro=detectar_registro(limpio),
        numeros=extraer_numeros(limpio, dominio_objetivo),
        pistas=pistas_cualitativas(limpio, dominio_objetivo),
    )
    return normalizado
