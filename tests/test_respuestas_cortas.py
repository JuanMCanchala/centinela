"""Una respuesta corta no es audio degradado.

Regresion del fallo que rompia la conversacion entera. `SenalCanal.degradado`
consideraba danado todo turno con menos de 12 caracteres utiles. En un
cuestionario clinico las respuestas mas frecuentes son exactamente esas:

    "No"          3 caracteres
    "Un seis"     7
    "Normal"      6
    "Si senora"   9

El agente marcaba cada una como audio degradado, respondia *"perdone, no le
escuche bien"*, y como los turnos degradados no alimentan el estado clinico, el
dominio nunca se llenaba y volvia a preguntar lo mismo. Bucle infinito en el
primer dominio, con la transcripcion funcionando perfectamente.

Lo corto no es lo mismo que lo danado. Estos tests fijan esa distincion, y ademas
comprueban la red de seguridad que impide el bucle aunque la heuristica falle.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from centinela.clinical.normalizer import limpiar_ruido, normalizar_turno  # noqa: E402
from centinela.clinical.triage_engine import TriageEngine  # noqa: E402
from centinela.dialog.guardrails import Intencion, clasificar  # noqa: E402
from centinela.dialog.policy import (  # noqa: E402
    MAX_REPETICIONES_SEGUIDAS,
    DialogPolicy,
    Paciente,
)
from centinela.clinical.extractor import ResultadoExtraccion  # noqa: E402

# Respuestas reales y breves de un paciente. Ninguna es audio degradado.
RESPUESTAS_CORTAS = (
    "No",
    "Si",
    "Un seis",
    "Como un cuatro",
    "Normal",
    "Si senora",
    "Si se oye",
    "Claro que si",
    "No he tenido",
    "Bien",
    "Mas o menos",
    "37.5",
)


@pytest.mark.parametrize("texto", RESPUESTAS_CORTAS)
def test_respuesta_corta_no_es_audio_degradado(texto: str) -> None:
    _limpio, senal = limpiar_ruido(texto)
    assert not senal.degradado, (
        f"respuesta corta legitima marcada como audio degradado: {texto!r} "
        f"({senal.caracteres_utiles} caracteres)"
    )


@pytest.mark.parametrize("texto", RESPUESTAS_CORTAS)
def test_respuesta_corta_se_clasifica_como_respuesta(texto: str) -> None:
    n = normalizar_turno(texto)
    cls = clasificar(
        texto,
        habla_tercero=n.registro.habla_tercero,
        audio_degradado=n.canal.degradado,
    )
    assert cls.intencion is not Intencion.AUDIO_DEGRADADO, (
        f"{texto!r} se clasifico como audio degradado"
    )


# Turnos que SI estan danados de verdad: el ruido se comio el contenido.
TURNOS_DANADOS = (
    "[inaudible]",
    "[inaudible] [inaudible] [inaudible]",
    "[silencio]",
    "[inaudible] [inaudible] es- [inaudible] no [inaudible] se- [inaudible]",
)


@pytest.mark.parametrize("texto", TURNOS_DANADOS)
def test_turno_danado_si_se_detecta(texto: str) -> None:
    _limpio, senal = limpiar_ruido(texto)
    assert senal.degradado, f"turno realmente danado no detectado: {texto!r}"


def test_turno_largo_con_algo_de_ruido_no_es_degradado() -> None:
    """Un inaudible aislado en una frase larga no invalida el turno.

    Es el caso de la capa 2 del dataset: se entiende lo esencial y descartarlo
    seria perder informacion clinica que si esta.
    """

    texto = "El dolor esta como en un [inaudible] seis y la herida se ve rojita"
    _limpio, senal = limpiar_ruido(texto)
    assert not senal.degradado


# --------------------------------------------------------------------------
# Red de seguridad: nunca un bucle infinito
# --------------------------------------------------------------------------

class ExtractorNulo:
    """No extrae nada: simula el peor caso para la maquina de estados."""

    async def extraer(self, texto_paciente, estado, turno_idx, pregunta_agente="",
                      dominio_objetivo=""):
        return ResultadoExtraccion(
            estado=estado, normalizado=normalizar_turno(texto_paciente), respondio=False
        )


@pytest.mark.asyncio
async def test_nunca_pide_repetir_indefinidamente() -> None:
    """Con audio degradado siempre, el agente avanza en vez de atascarse.

    Independiente de la heuristica: aunque `degradado` volviera a equivocarse, la
    conversacion progresa. Un agente que puede quedarse atascado por un solo juicio
    equivocado es un agente mal disenado, y atascarse aqui significa no llegar a
    preguntar por los sintomas que faltan.
    """

    policy = DialogPolicy(
        paciente=Paciente(
            paciente_id="pac_test", nombre="Paciente Test",
            procedimiento="Apendicectomía", dia_postop=7,
        ),
        extractor=ExtractorNulo(),
        motor=TriageEngine(),
        responder_clinico=None,
    )
    policy.abrir()
    await policy.procesar("Si, soy yo")

    dominio_inicial = policy._dominio_actual()
    dominios_vistos = {dominio_inicial}

    # Muchos turnos irreconocibles seguidos.
    for _ in range(MAX_REPETICIONES_SEGUIDAS + 4):
        await policy.procesar("[inaudible] [inaudible] [inaudible] [inaudible]")
        dominios_vistos.add(policy._dominio_actual())

    assert policy._dominio_actual() != dominio_inicial or len(dominios_vistos) > 1, (
        f"el agente se quedo atascado en el dominio {dominio_inicial!r} "
        f"pidiendo repetir indefinidamente"
    )


@pytest.mark.asyncio
async def test_un_turno_entendido_reinicia_el_contador() -> None:
    """Dos malos, uno bueno, dos malos: no debe saltarse el dominio."""

    policy = DialogPolicy(
        paciente=Paciente(
            paciente_id="pac_test", nombre="Paciente Test",
            procedimiento="Apendicectomía", dia_postop=7,
        ),
        extractor=ExtractorNulo(),
        motor=TriageEngine(),
        responder_clinico=None,
    )
    policy.abrir()
    await policy.procesar("Si, soy yo")

    await policy.procesar("[inaudible] [inaudible] [inaudible] [inaudible]")
    assert policy.repeticiones_seguidas == 1
    await policy.procesar("Un seis")
    assert policy.repeticiones_seguidas == 0, (
        "un turno entendido debe reiniciar el contador de repeticiones"
    )
