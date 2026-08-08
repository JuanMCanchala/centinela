"""El mismo texto, escrito como hay que leerlo en voz alta.

Piper fonemiza con espeak-ng, que lee las cifras como las leeria una calculadora.
Comprobado con `piper --debug` sobre el texto que el agente dice hoy:

    "fiebre de 38.5 grados"  ->  fjˈeβɾe ðe tɾˌeɪntaiˈotʃo pˌunto sˈinko ɣɾˈaðos
                                 "treinta y ocho PUNTO cinco"
    "llame al 123"           ->  ʝˈame al sjˈento βˌeɪntitɾˈes
                                 "llame al CIENTO VEINTITRES"

El segundo caso no es un defecto de estilo. `CIERRE_ROJO` es la frase que le da al
paciente la linea de emergencias, y decirsela como "ciento veintitres" le entrega un
numero que no va a reconocer, en el unico momento de la llamada en que eso importa.

**Donde se aplica y donde no.** Solo en la capa de voz, justo antes de sintetizar. El
registro clinico, la hoja de traspaso y el texto que se muestra en pantalla siguen
diciendo "38.5", que es lo que corresponde en un expediente. La cifra se conserva; lo
unico que cambia es como se pronuncia.

**Por que la tabla es corta a proposito.** Una normalizacion agresiva sobre texto
clinico puede convertir una cifra en otra: "IV" es a la vez numero romano y via
intravenosa, y "1/2" es una fraccion o una dosis segun el contexto. Aqui solo entra lo
que no tiene lectura ambigua. Que una unidad rara se lea deletreada es un defecto
menor; que una dosis se lea cambiada no lo es.
"""

from __future__ import annotations

import re

# Lineas telefonicas que se dicen digito a digito. Solo se sustituyen cuando van
# precedidas de algo que las declara como numero de telefono ("al 123", "linea 123"),
# para que un "123" que fuera una cantidad siga leyendose como cantidad.
LINEAS = {
    "123": "uno dos tres",
    "125": "uno dos cinco",
}

_RE_LINEA = re.compile(
    r"\b(al|a la|l[ií]nea|n[uú]mero|marque)\s+(" + "|".join(LINEAS) + r")\b",
    re.IGNORECASE,
)

# Decimal con punto o con coma. El grupo de la parte entera se conserva tal cual: espeak
# lee bien los enteros ("38" -> "treinta y ocho"), asi que no hace falta un conversor de
# numeros a palabras, que seria mucho mas codigo y mucho mas donde equivocarse.
_RE_DECIMAL = re.compile(r"\b(\d{1,3})[.,](\d{1,2})\b")

# Rango de cifras: el guion se lee como pausa o no se lee.
_RE_RANGO = re.compile(r"\b(\d{1,3})\s*[-–]\s*(\d{1,3})\b")

# Fraccion de escala ("7/10"), por si llega del corpus en vez de construida.
_RE_ESCALA = re.compile(r"\b(\d{1,2})\s*/\s*(10|100)\b")

# Unidades y abreviaturas sin lectura ambigua.
UNIDADES = (
    ("°C", " grados"),
    ("ºC", " grados"),
    ("°", " grados"),
    ("mmHg", " milimetros de mercurio"),
    ("mg", " miligramos"),
    ("ml", " mililitros"),
    ("cm", " centimetros"),
    ("mm", " milimetros"),
    ("kg", " kilos"),
    ("%", " por ciento"),
)

_RE_UNIDAD = re.compile(
    r"(?<=\d)\s?(" + "|".join(re.escape(u) for u, _ in UNIDADES) + r")"
)
_TRADUCCION_UNIDAD = dict(UNIDADES)

ABREVIATURAS = (
    (r"\bDr\.", "doctor"),
    (r"\bDra\.", "doctora"),
    (r"\baprox\.", "aproximadamente"),
    (r"\bhrs?\b", "horas"),
)

# Espacios sobrantes, que aparecen al sustituir.
_RE_ESPACIOS = re.compile(r"[ \t]{2,}")


def _decimal(m: re.Match) -> str:
    """La parte decimal como la dice una persona, no como la lee una maquina."""

    entero, decimal = m.group(1), m.group(2).rstrip("0")

    if not decimal:
        # 38.0 grados es 38 grados. Decir "treinta y ocho punto cero" no informa.
        dicho = entero
    elif decimal == "5":
        dicho = f"{entero} y medio"
    else:
        dicho = f"{entero} con {decimal}"
    return dicho


def _unidad(m: re.Match) -> str:
    return _TRADUCCION_UNIDAD[m.group(1)]


def _linea(m: re.Match) -> str:
    return f"{m.group(1)} {LINEAS[m.group(2)]}"


def para_voz(texto: str) -> str:
    """El texto listo para el fonemizador. No modifica el sentido, solo la lectura."""

    salida = texto

    if salida:
        salida = _RE_LINEA.sub(_linea, salida)
        salida = _RE_ESCALA.sub(lambda m: f"{m.group(1)} sobre {m.group(2)}", salida)
        # La unidad va ANTES del decimal: su patron exige un digito justo delante, y
        # "38.5 °C" ya no lo tiene una vez convertido en "38 y medio °C".
        salida = _RE_UNIDAD.sub(_unidad, salida)
        salida = _RE_DECIMAL.sub(_decimal, salida)
        salida = _RE_RANGO.sub(lambda m: f"{m.group(1)} a {m.group(2)}", salida)
        for patron, reemplazo in ABREVIATURAS:
            salida = re.sub(patron, reemplazo, salida)
        salida = _RE_ESPACIOS.sub(" ", salida).strip()

    return salida
