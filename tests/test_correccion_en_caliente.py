"""«No, dije treinta y ocho y medio, no treinta y cinco.»

Corregirse a mitad de frase es de las cosas mas humanas que hay en una llamada, y hasta
ahora el sistema la manejaba de la peor forma posible: `extractor._asignar` sobrescribia
la observacion entera. El valor viejo desaparecia sin rastro y el agente seguia como si
nada, asi que el paciente tampoco sabia si su correccion se habia registrado.

Se perdian dos cosas, y la segunda es la que importa:

  1. El valor anterior.
  2. **El hecho de que hubo una correccion**, que es un dato clinico por si mismo:
     significa que alguien de este lado entendio mal, o que el paciente cambio su
     reporte. Las dos cosas las quiere saber quien reciba el caso.

Y hay una forma de equivocarse al arreglarlo que seria peor que el problema: **que una
correccion retire una alerta ya creada**. Si bastara decir un numero mas bajo para
apagar el ticket, la correccion seria una puerta trasera a la criticidad. El paciente
que minimiza es uno de los perfiles del reto, no una hipotesis. Eso es lo que prueba el
ultimo bloque.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from centinela.clinical.extractor import Extractor, ResultadoExtraccion  # noqa: E402
from centinela.clinical.normalizer import normalizar_turno  # noqa: E402
from centinela.clinical.triage_engine import TriageEngine  # noqa: E402
from centinela.dialog import script as S  # noqa: E402
from centinela.dialog.policy import DialogPolicy, Paciente  # noqa: E402
from centinela.escalation.service import EscalationService  # noqa: E402
from centinela.models import ClinicalState, Nivel  # noqa: E402


class ExtractorDeTemperatura:
    """Anota la temperatura que aparezca en el turno, con el asignador de verdad.

    Usa `Extractor._asignar` sin tocarlo, porque es ahi donde vive el registro de la
    correccion y probar una copia no probaria nada.
    """

    def __init__(self) -> None:
        self.real = Extractor(llm=None)

    async def extraer(self, texto_paciente, estado, turno_idx, pregunta_agente="",
                      dominio_objetivo="", **_):
        norm = normalizar_turno(texto_paciente, dominio_objetivo)
        t = norm.numeros.temperatura_c
        if t is not None:
            self.real._asignar(estado, "fiebre", t, turno_idx, texto_paciente, False)
        return ResultadoExtraccion(estado=estado, normalizado=norm, respondio=True)


def nueva_policy() -> DialogPolicy:
    policy = DialogPolicy(
        paciente=Paciente(
            paciente_id="pac_test", nombre="Paciente Test",
            procedimiento="Colecistectomía", dia_postop=7,
        ),
        extractor=ExtractorDeTemperatura(),
        motor=TriageEngine(),
    )
    policy.abrir()
    return policy


# ==========================================================================
# El rastro
# ==========================================================================

def test_asignar_dos_veces_deja_las_dos_versiones() -> None:
    extractor = Extractor(llm=None)
    estado = ClinicalState()

    extractor._asignar(estado, "fiebre", 35.8, 2, "treinta y cinco ocho", False)
    extractor._asignar(estado, "fiebre", 38.5, 4, "no, treinta y ocho y medio", False)

    assert estado.fiebre_c.valor == 38.5, "el vigente es el ultimo"
    assert len(estado.correcciones) == 1
    c = estado.correcciones[0]
    assert (c.dominio, c.valor_anterior, c.valor_nuevo, c.turno_idx) == (
        "fiebre", 35.8, 38.5, 4
    )
    assert "treinta y ocho" in c.cita_paciente


def test_repetir_el_mismo_valor_no_es_una_correccion() -> None:
    """El paciente repite lo mismo. No hay nada que corregir ni que anunciar."""

    extractor = Extractor(llm=None)
    estado = ClinicalState()

    extractor._asignar(estado, "fiebre", 38.5, 2, "treinta y ocho y medio", False)
    extractor._asignar(estado, "fiebre", 38.5, 3, "si, treinta y ocho y medio", False)

    assert estado.correcciones == []


def test_el_primer_valor_no_es_una_correccion() -> None:
    extractor = Extractor(llm=None)
    estado = ClinicalState()

    extractor._asignar(estado, "fiebre", 37.2, 2, "treinta y siete dos", False)

    assert estado.correcciones == []


def test_tres_valores_dejan_dos_correcciones() -> None:
    extractor = Extractor(llm=None)
    estado = ClinicalState()

    extractor._asignar(estado, "dolor", 3.0, 1, "un tres", False)
    extractor._asignar(estado, "dolor", 6.0, 2, "no, un seis", False)
    extractor._asignar(estado, "dolor", 4.0, 3, "bueno, un cuatro", False)

    assert [(c.valor_anterior, c.valor_nuevo) for c in estado.correcciones] == [
        (3.0, 6.0), (6.0, 4.0)
    ]


# ==========================================================================
# El agente lo dice en voz alta
# ==========================================================================

@pytest.mark.asyncio
async def test_el_agente_acusa_la_correccion_y_repite_el_valor() -> None:
    """Cambiar el dato en silencio es lo que hace un formulario, no una persona."""

    policy = nueva_policy()
    await policy.procesar("Si, soy yo")
    await policy.procesar("no, dolor no, pero la temperatura fue de 36.5")

    tras = await policy.procesar("perdon, era 37.2 no 36.5")

    claves = [f.clave for f in tras.fragmentos]
    assert S.ACUSE_CORRECCION.clave in claves
    assert "fiebre de 37.2 grados" in tras.texto_completo
    assert policy.estado.fiebre_c.valor == 37.2


@pytest.mark.asyncio
async def test_sin_correccion_el_acuse_es_el_de_siempre() -> None:
    policy = nueva_policy()
    await policy.procesar("Si, soy yo")

    tras = await policy.procesar("la temperatura fue de 36.5")

    assert S.ACUSE_CORRECCION.clave not in [f.clave for f in tras.fragmentos]


@pytest.mark.asyncio
async def test_no_se_dicen_el_acuse_normal_y_el_de_correccion_a_la_vez() -> None:
    policy = nueva_policy()
    await policy.procesar("Si, soy yo")
    await policy.procesar("la temperatura fue de 36.5")

    tras = await policy.procesar("perdon, era 37.2")

    acuses_normales = [f for f in tras.fragmentos if f.clave in {a.clave for a in S.ACUSES}]
    assert acuses_normales == [], "uno o el otro, no los dos"


# ==========================================================================
# Una correccion NO retira una alerta
# ==========================================================================

@pytest.mark.asyncio
async def test_bajar_la_cifra_no_retira_la_bandera_ya_creada() -> None:
    """El caso adversarial: "tenia 38.5"... "no, dije 35.8"."""

    policy = nueva_policy()
    await policy.procesar("Si, soy yo")
    bandera = await policy.procesar("si, tuve fiebre de 38.5")

    assert bandera.decision.nivel is Nivel.ROJO
    assert bandera.escala_ahora is True, "el ticket ya salio en este turno"

    # Y ahora se desdice. La confirmacion estaba en el aire, asi que esto es un
    # desmentido Y una correccion a la vez.
    await policy.procesar("no, dije 35.8, me equivoque")

    assert policy.escalado is True, "la bandera no se puede desescalar hablando"
    assert [(c.valor_anterior, c.valor_nuevo) for c in policy.estado.correcciones] == [
        (38.5, 35.8)
    ]
    assert policy.confirmaciones[-1]["desenlace"] == "desmentido"


@pytest.mark.asyncio
async def test_la_hoja_de_traspaso_lleva_las_dos_versiones(tmp_path) -> None:
    policy = nueva_policy()
    await policy.procesar("Si, soy yo")
    await policy.procesar("si, tuve fiebre de 38.5")
    await policy.procesar("no, dije 35.8, me equivoque")

    servicio = EscalationService(tmp_path)
    decision = policy.decisiones[-1]
    hoja = servicio.hoja_legible(policy, decision, [], "llamada-de-prueba")

    assert "DATOS QUE EL PACIENTE CORRIGIO" in hoja
    assert "38.5 -> 35.8" in hoja
    assert "LO QUE EL PACIENTE NO CONFIRMO" in hoja
    assert "lo verifica una persona" in hoja


@pytest.mark.asyncio
async def test_el_resumen_estructurado_lleva_las_correcciones(tmp_path) -> None:
    policy = nueva_policy()
    await policy.procesar("Si, soy yo")
    await policy.procesar("si, tuve fiebre de 38.5")
    await policy.procesar("no, dije 35.8, me equivoque")

    servicio = EscalationService(tmp_path)
    resumen = servicio.construir_resumen(
        "llamada-de-prueba", policy, policy.decisiones[-1], []
    )

    correcciones = resumen["_centinela"]["correcciones"]
    assert len(correcciones) == 1
    assert correcciones[0]["valor_anterior"] == 38.5
    assert correcciones[0]["valor_nuevo"] == 35.8
