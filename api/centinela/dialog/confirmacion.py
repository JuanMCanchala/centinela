"""Leer de vuelta lo que se entendio, antes de actuar sobre ello.

En clinica esto se llama comunicacion de circuito cerrado, y no es cortesia: existe
porque el canal se equivoca. Aqui el canal tiene dos fuentes de error propias, y
ninguna de las dos se puede detectar mirando el texto:

  1. **El reconocedor.** Whisper oye "treinta y ocho" donde el paciente dijo "treinta
     y seis". El texto resultante es perfectamente gramatical y perfectamente falso.
  2. **El extractor.** El paciente dice "me duele bastante" y el sistema anota un 7.
     Ese 7 no lo dijo nadie: es una inferencia razonable de la que despues cuelga una
     decision clinica. `Procedencia.inferido` ya la marca desde el primer dia; lo que
     faltaba era hacer algo con la marca.

Lo unico que atrapa las dos es preguntar. Y hay un caso en el que preguntar es
obligatorio: cuando lo entendido va a **escalar la llamada**. Un rojo saca a alguien de
su casa camino de urgencias, y eso merece la misma frase que usa cualquier enfermera
antes de colgar el telefono.

**Dos razones y no tres.** El tercer candidato obvio -- confirmar cuando el audio venia
degradado -- no puede ocurrir: un turno degradado no alimenta el estado clinico
(`policy.SIN_CONTENIDO_CLINICO`), asi que no hay ningun valor que leer de vuelta. Ese
caso ya lo cubre `_responder_audio_degradado`, que pide repetir. Dejarlo aqui seria un
mando desconectado, y un mando desconectado es peor que no tener mando.

**Que NO hace este modulo.** No decide si la alerta se crea: eso ya paso. El ticket
nace en el turno en que aparece la bandera, y confirmar no lo retrasa ni lo retira.
Un desmentido se anota junto a la alerta y quien decide es una persona. Si el
desmentido pudiera apagar el ticket, "no, ya se me quito" seria una puerta trasera
para bajar la criticidad -- y el paciente que minimiza es uno de los perfiles del
reto, no una hipotesis.

Puro y sin E/S, como `completitud.py`: recibe estado y devuelve texto o veredicto. Asi
la politica se prueba sin STT y el arnes ejercita el mismo codigo que la llamada.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..clinical.normalizer import normalizar_turno, sin_tildes
from ..models import ClinicalState, Nivel, TriageDecision
from .guardrails import Intencion, clasificar

# Motivos por los que se lee de vuelta. Van al log: distinguir "confirme porque iba a
# escalar" de "confirme porque me lo invente" es lo que permite afinar el criterio con
# datos en vez de con impresiones.
MOTIVO_ESCALA = "va_a_escalar"
MOTIVO_INFERIDO = "valor_inferido"

# Como se dice cada dominio en voz alta. El texto de la regla no sirve: "fiebre igual o
# mayor a 38.0 grados centigrados" es el enunciado de un umbral, no algo que una persona
# le diga a otra por telefono.
# Van con tildes porque esto se pronuncia: sin ellas, espeak-ng pone la tonica donde
# no va ("esta" en vez de "está"). Los diccionarios de mas abajo -- los que reconocen
# lo que dice el PACIENTE -- van sin tildes a proposito, porque se comparan contra la
# salida del normalizador, que las quita.
_HERIDA = {
    "secrecion_purulenta": "líquido amarillo o pus saliendo de la herida",
    "eritema_leve": "la herida un poco enrojecida",
    "normal": "la herida sin novedad",
}

_MOVILIDAD = {
    "incapacitante_nueva": "que no puede caminar como antes",
    "limitada_esperada": "que se mueve con algo de dificultad",
    "normal": "que se mueve normal",
}

_APETITO = {
    "muy_disminuido": "que casi no está comiendo",
    "levemente_disminuido": "que está comiendo menos",
    "normal": "que está comiendo normal",
}

_SUENO = {
    "muy_alterado": "que casi no está durmiendo",
    "levemente_alterado": "que duerme peor de lo normal",
    "normal": "que duerme normal",
}

_POR_CATEGORIA = {
    "herida": _HERIDA,
    "movilidad": _MOVILIDAD,
    "apetito": _APETITO,
    "sueno": _SUENO,
}

# Respuestas a "es correcto?". Se resuelven aqui y no con `clasificar`, que responde a
# otra pregunta -- que CLASE de turno es este -- y no a si el paciente asintio.
_AFIRMA = (
    "si", "sip", "claro", "exacto", "correcto", "asi es", "eso es", "afirmativo",
    "de acuerdo", "efectivamente", "tal cual", "confirmo", "esta bien", "aja",
    "eso mismo", "seguro", "por supuesto", "obvio", "asi mismo",
)

_NIEGA = (
    "no", "nop", "negativo", "incorrecto", "para nada", "nada que ver",
    "me equivoque", "esta mal", "no es asi", "no dije", "que no", "eso no",
    "no era", "no fue", "mal",
)


@dataclass(frozen=True)
class Dato:
    """Un dato entendido, dicho como se lo dice a una persona."""

    dominio: str
    frase: str


@dataclass(frozen=True)
class Confirmacion:
    """Lo que hay que leer de vuelta, y por que."""

    datos: tuple[Dato, ...]
    motivo: str

    @property
    def frase(self) -> str:
        """Los datos enlazados como los enlazaria alguien hablando."""

        partes = [d.frase for d in self.datos]
        if len(partes) > 1:
            texto = ", ".join(partes[:-1]) + f" y {partes[-1]}"
        elif partes:
            texto = partes[0]
        else:
            texto = ""
        return texto

    @property
    def dominios(self) -> tuple[str, ...]:
        return tuple(d.dominio for d in self.datos)


def frase_de(dominio: str, valor) -> str:
    """El dato en castellano hablado. Cadena vacia si no hay nada que decir."""

    texto = ""
    if valor is not None:
        if dominio == "dolor":
            texto = f"un dolor de {_numero(valor)} sobre 10"
        elif dominio == "fiebre":
            texto = f"fiebre de {_numero(valor)} grados"
        else:
            tabla = _POR_CATEGORIA.get(dominio, {})
            texto = tabla.get(str(valor), "")
    return texto


def _numero(valor) -> str:
    """Sin el .0 de mas: "9" y no "9.0", "38.5" y no "38.50"."""

    if isinstance(valor, float) and valor.is_integer():
        texto = str(int(valor))
    else:
        texto = str(valor)
    return texto


def que_confirmar(
    estado: ClinicalState,
    decision: TriageDecision,
    turno_idx: int | None = None,
) -> Confirmacion | None:
    """Que hay que leerle de vuelta al paciente en este turno, si algo.

    El orden de las dos razones es su prioridad, y no es arbitrario: lo que va a
    escalar se confirma siempre, aunque el dato viniera explicito del paciente.

    `turno_idx` restringe la segunda razon a lo captado en ESTE turno. Sin ese filtro,
    un valor inferido en el turno dos se volveria a confirmar en el cinco.
    """

    confirmacion = None

    if decision is not None and decision.nivel is Nivel.ROJO:
        datos = []
        vistos = set()
        for regla in decision.reglas_rojas:
            frase = frase_de(regla.dominio, _valor_de(estado, regla))
            if frase and regla.dominio not in vistos:
                vistos.add(regla.dominio)
                datos.append(Dato(regla.dominio, frase))
        if datos:
            confirmacion = Confirmacion(tuple(datos), MOTIVO_ESCALA)

    if confirmacion is None:
        inferido = _dato_inferido(estado, turno_idx)
        if inferido is not None:
            confirmacion = Confirmacion((inferido,), MOTIVO_INFERIDO)

    return confirmacion


def hallazgos_hablados(estado: ClinicalState, reglas) -> list[str]:
    """Un hallazgo por elemento, sin unirlos todavia.

    Existe separado de `hallazgos_como_al_hablar` porque la VOZ los quiere sueltos. El anuncio
    de la bandera roja dice cada hallazgo en su propio fragmento, y eso es lo que permite que
    la voz clonada los diga: pre-renderizar los 37 hallazgos rojos sueltos es una suma, y
    pre-renderizar sus combinaciones unidas con comas y con "y" seria un producto que no cabe.

    Un hallazgo sin forma hablada se cae a su descripcion. Perder informacion clinica por
    sonar mejor no es un intercambio aceptable.
    """

    partes: list[str] = []
    vistos: set[str] = set()

    for regla in reglas:
        if regla.dominio not in vistos:
            vistos.add(regla.dominio)
            partes.append(frase_de(regla.dominio, _valor_de(estado, regla))
                          or regla.descripcion.lower())

    return partes


def hallazgos_como_al_hablar(estado: ClinicalState, reglas) -> str:
    """Las banderas rojas dichas en el idioma del paciente, no en el del protocolo.

    Existe porque el anuncio de la bandera le leia al paciente el enunciado del umbral:
    *"Lo que me describe -- fiebre igual o mayor a 38.0 grados centigrados -- es un signo
    de alarma"*. Eso esta escrito para el registro clinico y para la enfermera que lo
    revisa, no para decirselo a alguien por telefono.

    Y era incoherente consigo mismo: en el turno anterior el agente acaba de leer de
    vuelta "me dice fiebre de 38.5 grados", asi que decia lo mismo de dos maneras
    distintas en dos turnos seguidos.

    Si un hallazgo no tiene forma hablada se cae a su descripcion. Perder informacion
    clinica por sonar mejor no es un intercambio aceptable.
    """

    partes = hallazgos_hablados(estado, reglas)

    if len(partes) > 1:
        texto = ", ".join(partes[:-1]) + f" y {partes[-1]}"
    elif partes:
        texto = partes[0]
    else:
        texto = ""
    return texto


def _valor_de(estado: ClinicalState, regla) -> float | str | None:
    """El valor del estado, no el de la regla: es el que el paciente reporto.

    `RuleHit.valor_observado` suele coincidir, pero la fuente de verdad de lo que el
    paciente dijo es el estado clinico, y es lo que hay que repetirle.
    """

    valor = regla.valor_observado

    if regla.dominio:
        try:
            obs = estado.observacion(regla.dominio)
        except KeyError:
            # Un umbral con un dominio que `models` no conoce es un error de
            # programacion, pero el sitio donde estallaria es el anuncio de una bandera
            # roja: la llamada se caeria justo antes de mandar al paciente a urgencias.
            # Se cae al valor de la regla, que es informacion suficiente para el aviso.
            obs = None

        if obs is not None and not obs.falta:
            valor = obs.valor

    return valor


def _dato_inferido(estado: ClinicalState, turno_idx: int | None) -> Dato | None:
    """El dato de este turno cuyo valor no lo dijo el paciente sino el extractor.

    Solo los dos dominios numericos. Que la herida se deduzca de "esta como con un
    liquido amarillo" es su forma normal de funcionar y confirmar cada categoria
    duplicaria los turnos de la llamada sin ganar nada: lo que hay que confirmar es una
    CIFRA que nadie dijo, porque es la que despues se compara contra un umbral.
    """

    encontrado = None

    for dominio in ("fiebre", "dolor"):
        obs = estado.observacion(dominio)
        del_turno = (
            obs.procedencia is not None
            and (turno_idx is None or obs.procedencia.turno_idx == turno_idx)
        )
        if encontrado is None and not obs.falta and del_turno and obs.procedencia.inferido:
            frase = frase_de(dominio, obs.valor)
            if frase:
                encontrado = Dato(dominio, frase)

    return encontrado


# ==========================================================================
# La respuesta del paciente
# ==========================================================================

AFIRMA = "afirma"
NIEGA = "niega"
NI_UNA_COSA_NI_LA_OTRA = "no_resuelve"


def interpretar(texto: str, dominio: str = "") -> str:
    """Como respondio a "es correcto?": AFIRMA, NIEGA o ninguna de las dos.

    La negacion gana los empates. "no, si, o sea, no exactamente" es un desmentido con
    ruido, y tratarlo como un si seria dar por confirmado algo que el paciente esta
    discutiendo.

    Un turno que trae un dato NUEVO -- "no, era treinta y seis" -- niega y ademas
    corrige; quien llama se encarga de la correccion con el extractor de siempre, que
    es donde vive esa competencia.
    """

    base = sin_tildes((texto or "").strip().lower())
    limpio = base.replace(",", " ").replace(".", " ")
    palabras = limpio.split()
    veredicto = NI_UNA_COSA_NI_LA_OTRA

    if palabras:
        niega = _menciona(limpio, palabras, _NIEGA)
        afirma = _menciona(limpio, palabras, _AFIRMA)
        # "no" dentro de "no se" o "no me acuerdo" no es un desmentido: es un no se.
        if niega and _es_un_no_lo_se(limpio):
            niega = False
            afirma = False

        if niega:
            veredicto = NIEGA
        elif afirma:
            veredicto = AFIRMA
        else:
            # Sin si ni no, un dato nuevo del mismo dominio tambien es un desmentido:
            # el paciente esta corrigiendo la cifra en vez de discutirla.
            if dominio and _trae_otro_valor(texto, dominio):
                veredicto = NIEGA

    return veredicto


def _menciona(limpio: str, palabras: list[str], vocabulario) -> bool:
    """La marca aparece como palabra o como locucion, no como trozo de otra palabra."""

    sueltas = {p for p in palabras}
    encontrada = False
    for marca in vocabulario:
        if not encontrada:
            if " " in marca:
                encontrada = marca in limpio
            else:
                encontrada = marca in sueltas
    return encontrada


def _es_un_no_lo_se(limpio: str) -> bool:
    return any(
        frase in limpio
        for frase in ("no se", "no lo se", "no me acuerdo", "no recuerdo", "no sabria")
    )


def _trae_otro_valor(texto: str, dominio: str) -> bool:
    """El turno contiene un valor del dominio que se estaba confirmando."""

    norm = normalizar_turno(texto, dominio)
    if dominio == "dolor":
        trae = norm.numeros.dolor_nrs is not None
    elif dominio == "fiebre":
        trae = norm.numeros.temperatura_c is not None
    else:
        trae = norm.pistas.get(dominio) is not None
    return trae


def es_respuesta_a_la_confirmacion(texto: str) -> bool:
    """El turno se puede leer como un si o un no, y no como otra cosa.

    Existe porque el paciente puede no contestar la confirmacion: puede preguntar algo,
    puede pedir un humano, puede intentar manipular al agente. En esos casos manda el
    guardarrail de siempre y la confirmacion se da por no respondida.
    """

    norm = normalizar_turno(texto)
    cls = clasificar(
        texto,
        habla_tercero=norm.registro.habla_tercero,
        audio_degradado=norm.canal.degradado,
    )
    return cls.intencion in (Intencion.RESPUESTA, Intencion.HABLA_TERCERO)
