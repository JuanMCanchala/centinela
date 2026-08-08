"""Tipos del dominio clinico de Centinela.

Estos tipos son el contrato entre las dos mitades del agente:
la percepcion (LLM, difusa) produce `ClinicalState`; la decision
(codigo determinista) consume `ClinicalState` y produce `TriageDecision`.

El LLM nunca ve ni produce un `TriageDecision`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# Vocabularios cerrados. Coinciden exactamente con los valores del dataset
# oficial (trayectorias_postop_silver.xlsx) para que el arnes de replay
# pueda comparar campo a campo contra el ground truth.
# --------------------------------------------------------------------------

class Movilidad(str, Enum):
    NORMAL = "normal"
    LIMITADA_ESPERADA = "limitada_esperada"
    INCAPACITANTE_NUEVA = "incapacitante_nueva"


class Herida(str, Enum):
    NORMAL = "normal"
    ERITEMA_LEVE = "eritema_leve"
    SECRECION_PURULENTA = "secrecion_purulenta"


class Apetito(str, Enum):
    NORMAL = "normal"
    LEVEMENTE_DISMINUIDO = "levemente_disminuido"
    MUY_DISMINUIDO = "muy_disminuido"


class Sueno(str, Enum):
    NORMAL = "normal"
    LEVEMENTE_ALTERADO = "levemente_alterado"
    MUY_ALTERADO = "muy_alterado"


class Nivel(str, Enum):
    VERDE = "verde"
    AMARILLO = "amarillo"
    ROJO = "rojo"


DOMINIOS = ("dolor", "fiebre", "movilidad", "herida", "apetito", "sueno")

DominioClinico = Literal["dolor", "fiebre", "movilidad", "herida", "apetito", "sueno"]


# --------------------------------------------------------------------------
# Procedencia: cada dato clinico recuerda de que turno salio y con que
# palabras lo dijo el paciente. Es lo que permite que el resumen final y el
# ticket de escalamiento sean auditables sin volver a leer el audio.
# --------------------------------------------------------------------------

class Procedencia(BaseModel):
    turno_idx: int = Field(description="Turno de la llamada donde se capto el dato")
    cita_paciente: str = Field(description="Palabras textuales del paciente")
    inferido: bool = Field(
        default=False,
        description="True si el valor se derivo de lenguaje cualitativo en vez de un numero explicito",
    )


class Observacion(BaseModel):
    """Un campo clinico con su valor, su confianza y su procedencia."""

    valor: float | str | None = None
    conocido: bool = False
    procedencia: Procedencia | None = None

    @property
    def falta(self) -> bool:
        return not self.conocido or self.valor is None


class Correccion(BaseModel):
    """El paciente cambio un dato que ya habia dado.

    Existe porque el estado clinico se sobrescribia sin dejar rastro: "no, dije 38.5,
    no 35.8" reemplazaba el valor y la version anterior desaparecia. En una llamada
    clinica eso es perder informacion dos veces -- se pierde lo que dijo primero, y se
    pierde el hecho de que se corrigio, que es en si mismo un dato.

    La correccion NO retira una alerta ya creada. Si pudiera, seria la puerta trasera
    para bajar la criticidad: basta decir un numero mas bajo. El valor vigente se usa
    para decidir, las dos versiones quedan en el registro, y decide una persona.
    """

    turno_idx: int
    dominio: str
    valor_anterior: float | str | None
    valor_nuevo: float | str | None
    cita_paciente: str = ""


class ClinicalState(BaseModel):
    """El cuadro clinico tal como el agente lo ha podido reconstruir hablando.

    Todo campo puede faltar: el paciente no siempre responde lo que se le
    pregunta. Distinguimos explicitamente "no lo se" de "esta normal", porque
    tratar un desconocido como normal es el mecanismo tipico del falso negativo.
    """

    dolor_nrs: Observacion = Field(default_factory=Observacion)
    fiebre_c: Observacion = Field(default_factory=Observacion)
    movilidad: Observacion = Field(default_factory=Observacion)
    herida: Observacion = Field(default_factory=Observacion)
    apetito: Observacion = Field(default_factory=Observacion)
    sueno: Observacion = Field(default_factory=Observacion)

    # Senales que no son un dominio pero cambian la decision.
    fiebre_subjetiva: bool = Field(
        default=False,
        description="El paciente refiere sensacion febril o escalofrios sin haberse medido",
    )
    tiene_termometro: bool | None = None
    fiebre_negada: bool = Field(
        default=False,
        description=(
            "El paciente respondio explicitamente que no ha tenido fiebre. Es una "
            "respuesta, no una medicion: `fiebre_c` sigue vacia a proposito."
        ),
    )
    sintomas_libres: list[str] = Field(
        default_factory=list,
        description="Sintomas mencionados fuera de los seis dominios del protocolo",
    )
    correcciones: list[Correccion] = Field(
        default_factory=list,
        description=(
            "Valores que el paciente cambio a mitad de llamada. La observacion guarda "
            "el vigente; aqui queda el rastro de los anteriores."
        ),
    )

    def observacion(self, dominio: str) -> Observacion:
        return getattr(self, "sueno" if dominio == "sueno" else _CAMPO_POR_DOMINIO[dominio])

    def dominios_faltantes(self) -> list[str]:
        """Dominios sobre los que todavia no hay respuesta del paciente.

        La fiebre tiene una excepcion, y es deliberada. Es el unico dominio
        numerico cuya respuesta normal no produce un numero: cuando el paciente
        dice "no he tenido fiebre" no hay temperatura que guardar, pero SI hay
        respuesta. Contarla como ausente hacia que toda llamada normal cerrara en
        amarillo por "dominio sin indagar", con cero banderas -- un falso positivo
        en el caso mas comun de todos.

        Ojo con lo que esto NO hace: no inventa una temperatura. `fiebre_c` sigue
        vacia y el resumen lo declara con su motivo. Lo unico que cambia es que la
        pregunta cuenta como hecha y respondida.
        """

        faltan = []
        for dominio in DOMINIOS:
            if self.observacion(dominio).falta:
                negada = dominio == "fiebre" and self.fiebre_negada
                if not negada:
                    faltan.append(dominio)
        return faltan


_CAMPO_POR_DOMINIO = {
    "dolor": "dolor_nrs",
    "fiebre": "fiebre_c",
    "movilidad": "movilidad",
    "herida": "herida",
    "apetito": "apetito",
    "sueno": "sueno",
}


# --------------------------------------------------------------------------
# Decision
# --------------------------------------------------------------------------

class Cita(BaseModel):
    """Referencia verificable a un documento del corpus."""

    documento: str
    documento_sha256: str | None = None
    pagina: int | None = None
    cita_textual: str | None = None
    puntaje: float | None = None


class RuleHit(BaseModel):
    """Una regla que se disparo, con el dato que la disparo y su respaldo."""

    codigo: str
    descripcion: str
    dominio: str
    valor_observado: float | str | None
    umbral: str
    cita: Cita | None = None


class TriageDecision(BaseModel):
    nivel: Nivel
    reglas_rojas: list[RuleHit] = Field(default_factory=list)
    banderas_amarillas: list[RuleHit] = Field(default_factory=list)
    dominios_por_indagar: list[str] = Field(default_factory=list)
    provisional: bool = Field(
        default=False,
        description="True cuando falta informacion para cerrar la decision; obliga a indagar",
    )
    motivo: str = ""
    version_reglas: str = ""
    evaluado_en: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def escala(self) -> bool:
        return self.nivel in (Nivel.AMARILLO, Nivel.ROJO)
