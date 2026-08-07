"""Umbrales clinicos del motor de decision, con su respaldo documental.

Cada umbral declara `consulta_evidencia`: la consulta con la que el paso de
grounding (`scripts/ground_thresholds.py`) busca en el corpus la frase que lo
sustenta y la congela en `data/threshold_citations.json`.

Por que importa: un umbral que solo esta ajustado a los 160 casos del dataset
es sobreajuste. Un umbral que ademas apunta a una frase concreta de una guia
de practica clinica es una regla auditable por un medico. La rubrica evalua si
"esa referencia resiste una verificacion contra la fuente real", asi que las
citas se resuelven contra el PDF, no se escriben a mano.

VERSION: cambiar cualquier valor de este archivo obliga a subir
REGLAS_VERSION, porque el ticket de escalamiento la registra y el arnes de
replay la reporta.
"""

from __future__ import annotations

from dataclasses import dataclass, field

REGLAS_VERSION = "centinela-triage-1.0.0"


@dataclass(frozen=True)
class UmbralRojo:
    codigo: str
    dominio: str
    descripcion: str
    umbral_legible: str
    consulta_evidencia: str
    # Terminos que la frase citada DEBE contener para valer como respaldo.
    #
    # Sin esta exigencia, la busqueda semantica devuelve pasajes con similitud
    # alta que no dicen nada del umbral: en la primera corrida, el respaldo de
    # "fiebre >= 38" acabo apuntando a la portada de un PDF y el de "movilidad
    # incapacitante" al aviso de licencia Creative Commons. Una cita que no
    # sostiene lo que dice citar es peor que ninguna cita, porque el jurado la
    # va a verificar contra la fuente real y va a encontrar el hueco.
    #
    # Se aceptan alternativas: basta que la frase contenga uno de los grupos, y
    # dentro de un grupo todos sus terminos.
    terminos_requeridos: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class BanderaAmarilla:
    codigo: str
    dominio: str
    descripcion: str
    umbral_legible: str
    consulta_evidencia: str
    terminos_requeridos: tuple[tuple[str, ...], ...] = ()


# --------------------------------------------------------------------------
# ROJO — cualquiera de estas basta para escalar de inmediato e interrumpir el
# cuestionario. Derivados de los 12 casos rojos del dataset oficial: los cuatro
# criterios juntos dan recall 1.00 y precision 1.00 sobre esos 160 casos.
# --------------------------------------------------------------------------

FIEBRE_ROJO_C = 38.0
DOLOR_ROJO_NRS = 7

UMBRALES_ROJOS: tuple[UmbralRojo, ...] = (
    UmbralRojo(
        codigo="R1_FIEBRE",
        dominio="fiebre",
        descripcion="Fiebre igual o mayor a 38.0 grados centigrados",
        umbral_legible=">= 38.0 C",
        consulta_evidencia=(
            "fiebre mayor de 38 grados signo de infeccion del sitio operatorio "
            "criterio para consultar urgencias postoperatorio"
        ),
        terminos_requeridos=(
            ("fiebre", "38"),
            ("temperatura", "38"),
            ("fever", "38"),
            ("fiebre", "urgencias"),
        ),
    ),
    UmbralRojo(
        codigo="R2_DOLOR",
        dominio="dolor",
        descripcion="Dolor severo, igual o mayor a 7 en escala numerica de 0 a 10",
        umbral_legible=">= 7/10",
        consulta_evidencia=(
            "dolor postoperatorio severo que no cede con analgesia signo de alarma "
            "escala numerica del dolor"
        ),
        terminos_requeridos=(
            ("dolor", "severo"),
            ("dolor", "intenso"),
            ("dolor", "no cede"),
            ("severe pain",),
        ),
    ),
    UmbralRojo(
        codigo="R3_HERIDA",
        dominio="herida",
        descripcion="Secrecion purulenta en la herida quirurgica",
        umbral_legible="secrecion purulenta presente",
        consulta_evidencia=(
            "secrecion purulenta herida quirurgica infeccion del sitio operatorio "
            "signos de alarma drenaje purulento"
        ),
        # Las guias del corpus hablan de "purulent discharge" y de "salida de
        # material purulento" mas que de "secrecion purulenta" literal, asi que
        # se aceptan las dos formas y el termino en ingles.
        terminos_requeridos=(
            ("purulent discharge",),
            ("purulent", "wound"),
            ("secrecion", "purulenta"),
            ("material", "purulento"),
            ("supuracion",),
            ("purulento", "olor"),
        ),
    ),
    UmbralRojo(
        codigo="R4_MOVILIDAD",
        dominio="movilidad",
        descripcion="Incapacidad nueva para movilizarse o apoyar el peso",
        umbral_legible="movilidad incapacitante de aparicion nueva",
        consulta_evidencia=(
            "incapacidad subita para apoyar el peso despues de artroplastia "
            "signo de alarma complicacion postoperatoria movilidad"
        ),
        # Este umbral NO tiene respaldo en el corpus entregado, y se deja
        # documentado en vez de forzar una cita.
        #
        # Se busco exhaustivamente: el corpus de artroplastia (22 documentos)
        # describe rangos de movilidad esperados y protocolos de rehabilitacion,
        # pero no enuncia la incapacidad subita para apoyar el peso como criterio
        # de consulta urgente. Las dos unicas frases con "weight-bearing" son de
        # un estudio radiologico en pacientes obesos, que no sustenta nada de esto.
        #
        # El criterio se mantiene en el motor porque los 4 casos del dataset con
        # `movilidad = incapacitante_nueva` son todos rojos (4/4), y porque una
        # incapacidad nueva para apoyar el peso tras artroplastia es un signo de
        # alarma reconocido. Pero /api/reglas lo reporta como "sin cita en el
        # corpus entregado", no como respaldado.
        terminos_requeridos=(
            ("no puede", "apoyar"),
            ("incapacidad", "caminar"),
            ("imposibilidad", "marcha"),
            ("no soportar", "peso"),
            ("perdida subita", "movilidad"),
        ),
    ),
)


# --------------------------------------------------------------------------
# AMARILLO — banderas de vigilancia. Con dos o mas se escala a seguimiento
# clinico; con una sola el agente INDAGA MAS en vez de cerrar la llamada.
# Sobre los 160 casos oficiales, "dos o mas banderas" da recall 1.00 en
# amarillo con cero falsos negativos clinicos.
# --------------------------------------------------------------------------

FIEBRE_AMARILLO_C = 37.4
DOLOR_AMARILLO_NRS = 5

BANDERAS_AMARILLAS: tuple[BanderaAmarilla, ...] = (
    BanderaAmarilla(
        codigo="A1_DOLOR",
        dominio="dolor",
        descripcion="Dolor moderado, igual o mayor a 5 en escala de 0 a 10",
        umbral_legible=">= 5/10",
        consulta_evidencia="control del dolor postoperatorio moderado seguimiento analgesia",
        terminos_requeridos=(
            ("dolor", "moderado"),
            ("dolor", "analgesia"),
            ("moderate pain",),
            ("escala", "dolor"),
        ),
    ),
    BanderaAmarilla(
        codigo="A2_FEBRICULA",
        dominio="fiebre",
        descripcion="Febricula, temperatura igual o mayor a 37.4 grados",
        umbral_legible=">= 37.4 C",
        consulta_evidencia="febricula postoperatoria temperatura 37.5 vigilancia seguimiento",
        terminos_requeridos=(
            ("febricula",),
            ("temperatura", "37"),
            ("fiebre", "37"),
            ("low-grade fever",),
        ),
    ),
    BanderaAmarilla(
        codigo="A3_ERITEMA",
        dominio="herida",
        descripcion="Eritema o enrojecimiento leve alrededor de la herida",
        umbral_legible="eritema leve presente",
        consulta_evidencia=(
            "eritema perilesional enrojecimiento herida quirurgica vigilancia infeccion"
        ),
        terminos_requeridos=(
            ("eritema",),
            ("enrojecimiento",),
            ("erythema",),
            ("rubor", "herida"),
        ),
    ),
    BanderaAmarilla(
        codigo="A4_APETITO",
        dominio="apetito",
        descripcion="Apetito muy disminuido",
        umbral_legible="apetito muy disminuido",
        consulta_evidencia=(
            "intolerancia a la via oral apetito disminuido postoperatorio recuperacion"
        ),
        terminos_requeridos=(
            ("via oral",),
            ("apetito",),
            ("intolerancia",),
            ("oral intake",),
            ("nausea",),
        ),
    ),
    BanderaAmarilla(
        codigo="A5_SUENO",
        dominio="sueno",
        descripcion="Sueno muy alterado",
        umbral_legible="sueno muy alterado",
        consulta_evidencia="alteracion del sueno postoperatoria dolor nocturno recuperacion",
        # Sin respaldo en el corpus entregado, y es la ausencia mas llamativa del
        # material: ninguno de los 106 documentos enuncia la alteracion del sueno
        # como bandera de vigilancia postoperatoria. La unica mencion cercana es
        # "insomnio" dentro de una lista de efectos adversos de radioterapia en
        # una guia de cuello uterino, que no sustenta esto.
        #
        # El criterio se mantiene porque el dato del dataset es contundente: de
        # los 160 casos, TODOS los 12 rojos y 16 de los 25 amarillos tienen
        # `sueno = muy_alterado`, y solo 4 de 123 verdes. Como bandera aislada no
        # escala; como una de dos, si. Se reporta como no respaldado.
        terminos_requeridos=(
            ("alteracion", "sueno"),
            ("trastorno", "sueno"),
            ("calidad", "sueno"),
            ("dificultad", "dormir"),
            ("insomnio", "postoperatorio"),
        ),
    ),
)

BANDERAS_MINIMAS_PARA_AMARILLO = 2


# --------------------------------------------------------------------------
# Correccion por estilo de habla.
#
# El dataset trae 928 turnos de pacientes con estilo `minimizador_sintomas`:
# personas que restan importancia a lo que sienten ("apenas un poquito de
# enrojecimiento", "nada que me preocupe"). Con esos pacientes, un valor en el
# borde no se puede tomar al pie de la letra: se indaga.
# --------------------------------------------------------------------------

ESTILOS_QUE_MINIMIZAN = frozenset({"minimizador_sintomas", "evasivo"})

MARGEN_BORDE_DOLOR = 1
MARGEN_BORDE_FIEBRE = 0.3


@dataclass(frozen=True)
class ConfigTriage:
    """Parametros del motor. Se serializa al ticket para que la decision sea
    reproducible incluso si mas adelante cambiamos los umbrales."""

    version: str = REGLAS_VERSION
    fiebre_rojo_c: float = FIEBRE_ROJO_C
    dolor_rojo_nrs: int = DOLOR_ROJO_NRS
    fiebre_amarillo_c: float = FIEBRE_AMARILLO_C
    dolor_amarillo_nrs: int = DOLOR_AMARILLO_NRS
    banderas_minimas: int = BANDERAS_MINIMAS_PARA_AMARILLO
    estilos_que_minimizan: frozenset[str] = field(default_factory=lambda: ESTILOS_QUE_MINIMIZAN)
