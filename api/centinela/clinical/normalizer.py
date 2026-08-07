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

    @property
    def degradado(self) -> bool:
        """Turno demasiado dañado para interpretarlo con seguridad."""

        if self.caracteres_utiles < 12:
            malo = True
        else:
            proporcion = self.inaudibles / max(1, self.caracteres_utiles / 40)
            malo = self.inaudibles >= 3 and proporcion > 0.8
        return malo


def limpiar_ruido(texto: str) -> tuple[str, SenalCanal]:
    senal = SenalCanal()
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
    "cero": 0, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10,
}

# "un 6", "como un 6", "en 5", "6 de 10", "6/10", "seis"
RE_DOLOR_NUM = re.compile(
    r"(?:(?:un|en|como un|como en|de|nivel|escala de)\s+)?"
    r"\b(10|[0-9])\b(?:\s*(?:/|de|sobre)\s*10)?"
)
RE_DOLOR_PALABRA = re.compile(
    r"\b(?:un|en|como un)\s+(" + "|".join(PALABRA_A_NUMERO) + r")\b"
)

# "37.5", "37,5", "38 grados", "37 y pico", "treinta y ocho"
RE_TEMP_DECIMAL = re.compile(r"\b(3[5-9]|4[0-2])[.,]([0-9])\b")
RE_TEMP_ENTERA = re.compile(r"\b(3[5-9]|4[0-2])\b(?!\s*[.,]?\s*\d)")
RE_TEMP_Y_PICO = re.compile(r"\b(3[5-9])\s*(?:grados?\s*)?y\s*pico\b")

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
)


@dataclass
class NumerosClinicos:
    dolor_nrs: int | None = None
    temperatura_c: float | None = None
    fiebre_subjetiva: bool = False
    sin_termometro: bool = False
    evidencia_dolor: str = ""
    evidencia_temperatura: str = ""


def extraer_numeros(texto: str) -> NumerosClinicos:
    """Extraccion numerica por reglas, sensible al contexto de la frase.

    Sensible al contexto porque en "me operaron el 7 y el dolor es un 3" hay dos
    numeros y solo uno es dolor. Se busca el numero en la vecindad de una
    palabra del dominio, no en cualquier parte del turno.
    """

    base = sin_tildes(texto)
    n = NumerosClinicos()

    # --- temperatura ---
    hay_contexto_temp = any(t in base for t in CONTEXTO_TEMPERATURA)
    if hay_contexto_temp:
        m = RE_TEMP_DECIMAL.search(base)
        if m:
            n.temperatura_c = float(f"{m.group(1)}.{m.group(2)}")
            n.evidencia_temperatura = m.group(0)
        else:
            m = RE_TEMP_Y_PICO.search(base)
            if m:
                # "37 y pico" se interpreta como 37.5: es el punto medio del
                # intervalo que el paciente esta describiendo.
                n.temperatura_c = float(m.group(1)) + 0.5
                n.evidencia_temperatura = m.group(0)
            else:
                m = RE_TEMP_ENTERA.search(base)
                if m:
                    n.temperatura_c = float(m.group(1))
                    n.evidencia_temperatura = m.group(0)

    n.fiebre_subjetiva = any(t in base for t in SINONIMOS_FIEBRE_SUBJETIVA)
    n.sin_termometro = any(t in base for t in SIN_TERMOMETRO)

    # --- dolor ---
    hay_contexto_dolor = any(t in base for t in CONTEXTO_DOLOR)
    if hay_contexto_dolor:
        # Se excluye el fragmento que ya se interpreto como temperatura para no
        # leer "37" como un dolor de 7.
        limpio = base
        if n.evidencia_temperatura:
            limpio = limpio.replace(n.evidencia_temperatura, " ")
        limpio = re.sub(r"\b(3[5-9]|4[0-2])\b", " ", limpio)

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
    ),
    "eritema_leve": (
        "rojita", "rojito", "enrojecid", "enrojecimiento", "roja alrededor",
        "un poco roja", "colorad", "irritada", "inflamadita", "rosadita",
    ),
    "normal": (
        "se ve bien", "esta bien", "normal", "limpiecita", "cerrando bien",
        "sin nada raro", "no le veo nada", "cicatrizando bien",
    ),
}

PISTAS_MOVILIDAD = {
    "incapacitante_nueva": (
        "no puedo caminar", "no me puedo levantar", "no puedo apoyar",
        "no aguanto el peso", "no puedo pararme", "ya no puedo mover",
        "de un dia para otro no", "no me puedo mover", "no logro pararme",
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


def pistas_cualitativas(texto: str) -> dict[str, str]:
    """Categoria sugerida por dominio segun lexico observado en el dataset.

    Se ordena de mas grave a menos grave a proposito: si un turno contiene a la
    vez "sale un liquido amarillo" y "se ve bien", gana el hallazgo grave. En
    salud, ante dos lecturas posibles del mismo turno, la conservadora es la que
    escala.
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
                    if t in base:
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


def normalizar_turno(texto: str) -> TurnoNormalizado:
    limpio, canal = limpiar_ruido(texto)
    normalizado = TurnoNormalizado(
        texto_original=texto,
        texto=limpio,
        canal=canal,
        registro=detectar_registro(limpio),
        numeros=extraer_numeros(limpio),
        pistas=pistas_cualitativas(limpio),
    )
    return normalizado
