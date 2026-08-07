"""Lo que queda cuando la llamada termina.

La rubrica pide, textualmente, que al terminar la llamada exista *"un resumen que
identifique al paciente y su procedimiento, los sintomas reportados, la decision
tomada, las referencias usadas y los proximos pasos"*. Son seis cosas concretas y
enumerables, asi que se pueden comprobar una por una en vez de confiar en que
esten.

No habia ninguna prueba que lo hiciera. `eval/humo.py` recorre la API de extremo a
extremo y mira que el cierre responda, pero no abre el resumen para verificar que
los seis elementos estan dentro. Un resumen al que le falte "los proximos pasos"
pasaba todas las suites.

Esta prueba corre el cuestionario completo -- los seis dominios -- contra la
politica de verdad, cierra la llamada, y desarma el resumen persistido.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from centinela.clinical.extractor import Extractor  # noqa: E402
from centinela.clinical.triage_engine import TriageEngine  # noqa: E402
from centinela.dialog.policy import DialogPolicy, Paciente  # noqa: E402
from centinela.escalation.service import EscalationService  # noqa: E402
from centinela.llm.backend import UsoTokens  # noqa: E402
from centinela.models import Nivel  # noqa: E402

PACIENTE = Paciente(
    paciente_id="pac_42_00026",
    nombre="Ana Lucía Restrepo",
    procedimiento="Colecistectomía",
    dia_postop=7,
    edad=47,
    genero="F",
    comorbilidades=["diabetes_tipo_2"],
    ciudad="Medellín",
    eps="Sura EPS",
)

CITAS = [
    {
        "documento": "PLAN DE CUIDADO COLECISTECTOMIA.pdf",
        "pagina": 4,
        "cita_textual": "La secreción purulenta en el sitio quirúrgico requiere valoración médica.",
        "documento_sha256": "a" * 64,
        "similitud": 0.81,
    }
]

# Un turno por dominio, en el orden en que el agente pregunta. El ultimo dispara
# la bandera roja, que es el caso en que el resumen importa de verdad porque
# alguien lo va a leer para atender al paciente.
CONVERSACION = (
    "Sí, soy yo",
    "Como un seis",
    "Me tomé la temperatura y estaba en treinta y siete cinco",
    "Camino con dificultad pero camino",
    "Casi no me da hambre",
    "Duermo muy mal, me despierto por el dolor",
    "La herida tiene un líquido amarillo espeso y huele mal",
)


class ExtractorSinModelo(Extractor):
    """El extractor DE VERDAD, con la tercera capa neutralizada.

    La primera version de esta prueba traia un extractor de mentira que
    normalizaba el turno y devolvia el estado sin tocarlo. Los seis dominios
    quedaban vacios, la llamada cerraba en amarillo por dominios sin indagar, y el
    fallo parecia del sistema cuando era del test.

    Asi es mejor: las capas 1 y 2 -- reglas numericas y lexico cualitativo -- son
    el codigo real, y solo se anula la capa 3, la que invoca al modelo. La prueba
    no necesita Ollama levantado y de paso comprueba algo que vale la pena: que un
    cuestionario respondido con frases normales se resuelve sin modelo.
    """

    def __init__(self) -> None:
        super().__init__(llm=None)
        self.veces_que_quiso_el_modelo = 0

    async def _preguntar_al_modelo(self, texto, pregunta_agente, dominio_objetivo):
        self.veces_que_quiso_el_modelo += 1
        return {"datos": {}, "uso": UsoTokens()}


async def llamada_completa(tmp_path: Path) -> tuple[dict, dict | None, EscalationService, str]:
    servicio = EscalationService(tmp_path)
    policy = DialogPolicy(
        paciente=PACIENTE,
        extractor=ExtractorSinModelo(),
        motor=TriageEngine(),
        responder_clinico=None,
    )
    llamada_id = "llamada_de_prueba"
    servicio.registrar_inicio(llamada_id, policy)
    policy.abrir()

    for turno in CONVERSACION:
        await policy.procesar(turno)

    accion = policy.cerrar_ahora()
    decision = accion.decision or policy.decision_vigente
    cierre = servicio.cerrar_llamada(llamada_id, policy, decision, CITAS)
    return cierre["resumen"], cierre["ticket"], servicio, llamada_id


def recursos(resumen: dict, tipo: str) -> list[dict]:
    return [
        e["resource"]
        for e in resumen.get("entry", [])
        if e.get("resource", {}).get("resourceType") == tipo
    ]


# ==========================================================================
# Los seis elementos que exige la rubrica
# ==========================================================================

@pytest.mark.asyncio
async def test_1_identifica_al_paciente(tmp_path) -> None:
    resumen, _, servicio, _ = await llamada_completa(tmp_path)
    try:
        pacientes = recursos(resumen, "Patient")
        assert len(pacientes) == 1, "el resumen debe identificar exactamente un paciente"
        p = pacientes[0]
        assert p["id"] == PACIENTE.paciente_id
        assert PACIENTE.nombre in json.dumps(p, ensure_ascii=False)
    finally:
        servicio.cerrar()


@pytest.mark.asyncio
async def test_2_identifica_el_procedimiento_y_el_dia(tmp_path) -> None:
    resumen, _, servicio, _ = await llamada_completa(tmp_path)
    try:
        encuentros = recursos(resumen, "Encounter")
        assert encuentros, "falta el Encounter con el procedimiento"
        texto = json.dumps(encuentros, ensure_ascii=False)
        assert PACIENTE.procedimiento in texto
        assert "7" in texto, "el dia postoperatorio no aparece"
    finally:
        servicio.cerrar()


@pytest.mark.asyncio
async def test_3_registra_los_sintomas_reportados_con_la_cita_del_paciente(tmp_path) -> None:
    """Cada sintoma con su valor y con la frase textual que lo sustenta.

    La procedencia es la parte que no se puede dar por hecha: un valor clinico sin
    la frase del paciente que lo produjo no es auditable.
    """

    resumen, _, servicio, _ = await llamada_completa(tmp_path)
    try:
        obs = recursos(resumen, "Observation")
        dominios = {o["code"]["text"] for o in obs}
        assert dominios == {"dolor", "fiebre", "movilidad", "herida", "apetito", "sueno"}, (
            f"faltan dominios en el resumen: {dominios}"
        )

        resueltos = [o for o in obs if o["status"] == "final"]
        assert len(resueltos) >= 3, (
            f"solo {len(resueltos)} dominios quedaron resueltos tras responder los seis"
        )

        for o in resueltos:
            assert o["code"]["coding"][0]["code"], f"{o['code']['text']} sin código LOINC"
            assert o.get("note"), f"{o['code']['text']} sin la frase del paciente"
            assert o["note"][0]["text"].strip(), f"{o['code']['text']} con cita vacía"

        # Y los que no se resolvieron lo dicen, en vez de aparentar un valor.
        for o in obs:
            if o["status"] != "final":
                assert o.get("dataAbsentReason"), (
                    f"{o['code']['text']} no reportado y sin dataAbsentReason"
                )
    finally:
        servicio.cerrar()


@pytest.mark.asyncio
async def test_4_registra_la_decision_tomada(tmp_path) -> None:
    resumen, _, servicio, _ = await llamada_completa(tmp_path)
    try:
        riesgos = recursos(resumen, "RiskAssessment")
        assert riesgos, "falta el RiskAssessment con la decisión"
        texto = json.dumps(riesgos, ensure_ascii=False).lower()
        assert "rojo" in texto, "la decisión roja no quedó en el resumen"
        assert resumen["meta"]["versionReglas"], "sin versión de reglas no es reproducible"
    finally:
        servicio.cerrar()


@pytest.mark.asyncio
async def test_5_registra_las_referencias_usadas(tmp_path) -> None:
    resumen, _, servicio, _ = await llamada_completa(tmp_path)
    try:
        crudo = json.dumps(resumen, ensure_ascii=False)
        assert CITAS[0]["documento"] in crudo, "la referencia documental no quedó registrada"
        assert str(CITAS[0]["pagina"]) in crudo, "la página de la referencia no quedó"
    finally:
        servicio.cerrar()


@pytest.mark.asyncio
async def test_6_registra_los_proximos_pasos(tmp_path) -> None:
    resumen, _, servicio, _ = await llamada_completa(tmp_path)
    try:
        crudo = json.dumps(resumen, ensure_ascii=False)
        assert "proximos_pasos" in crudo, "el resumen no dice qué sigue"
        peticiones = recursos(resumen, "CommunicationRequest")
        assert peticiones, "falta la CommunicationRequest con lo que se le comunica al paciente"
        carga = peticiones[0]["payload"][0]["contentString"]
        assert carga.strip(), "los próximos pasos están vacíos"
    finally:
        servicio.cerrar()


# ==========================================================================
# Persistencia: el resumen sobrevive al proceso
# ==========================================================================

@pytest.mark.asyncio
async def test_el_resumen_queda_en_sqlite_y_se_puede_releer(tmp_path) -> None:
    """La rubrica pregunta "con que persistencia". Con esta."""

    resumen, _, servicio, llamada_id = await llamada_completa(tmp_path)
    try:
        fila = servicio.llamada(llamada_id)
        assert fila is not None, "la llamada no quedó persistida"
        assert fila["nivel_final"] == "rojo"
        assert fila["n_turnos"] >= len(CONVERSACION)
        assert fila["terminada_en"], "sin marca de cierre"
        assert fila["resumen"]["entry"], "el resumen persistido llegó vacío"
        # La transcripcion completa, para poder auditar la llamada sin el audio.
        assert "paciente" in fila["transcripcion"]
        assert "Como un seis" in fila["transcripcion"]
    finally:
        servicio.cerrar()


@pytest.mark.asyncio
async def test_la_alerta_roja_crea_ticket_con_hoja_legible(tmp_path) -> None:
    """El ticket es lo que una persona lee para atender al paciente."""

    _, ticket, servicio, llamada_id = await llamada_completa(tmp_path)
    try:
        assert ticket is not None, "una decisión roja tiene que crear ticket"
        assert ticket["nivel"] == "rojo"
        assert ticket["motivo"].strip()
        hoja = ticket["hoja_legible"]

        # Las cuatro cosas que quien recibe la alerta necesita sin abrir nada mas.
        for esperado in (PACIENTE.nombre, PACIENTE.procedimiento, "dolor", "herida"):
            assert esperado in hoja, f"la hoja de traspaso no menciona {esperado!r}"

        # Las reglas y la version se persisten pero NO viajan en la respuesta del
        # cierre: se leen por `tickets()`, que es lo que sirve /api/tickets. Se
        # comprueba por ahi, y de paso queda probado que sobreviven al proceso.
        guardados = servicio.tickets()
        assert guardados, "el ticket no quedó en la base"
        guardado = next(t for t in guardados if t["llamada_id"] == llamada_id)
        assert guardado["reglas"], "el ticket guardado no dice qué regla disparó"
        assert any(r.get("codigo", "").startswith("R") for r in guardado["reglas"]), (
            "una alerta roja debe llevar al menos una regla de alarma R*"
        )
        assert guardado["version_reglas"], "el ticket no fija la versión de reglas"
        assert guardado["estado"] == "abierto", "una alerta nueva nace abierta"
    finally:
        servicio.cerrar()


@pytest.mark.asyncio
async def test_una_llamada_verde_no_crea_ticket(tmp_path) -> None:
    """La otra cara: sin bandera no hay alerta, pero el resumen se guarda igual."""

    servicio = EscalationService(tmp_path)
    try:
        policy = DialogPolicy(
            paciente=PACIENTE, extractor=ExtractorSinModelo(),
            motor=TriageEngine(), responder_clinico=None,
        )
        servicio.registrar_inicio("verde_1", policy)
        policy.abrir()
        for turno in ("Sí, soy yo", "Un dos", "No, no he tenido fiebre",
                      "Camino normal", "Como normal", "Duermo bien",
                      "La herida está normal, sin nada raro"):
            await policy.procesar(turno)

        accion = policy.cerrar_ahora()
        decision = accion.decision or policy.decision_vigente
        cierre = servicio.cerrar_llamada("verde_1", policy, decision, [])

        assert decision.nivel is Nivel.VERDE, f"debió cerrar en verde, cerró en {decision.nivel}"
        assert cierre["ticket"] is None, "una llamada verde no debe crear alerta"
        assert cierre["resumen"]["entry"], "el resumen debe guardarse igual"
        assert servicio.llamada("verde_1")["nivel_final"] == "verde"
    finally:
        servicio.cerrar()


@pytest.mark.asyncio
async def test_el_resumen_es_json_serializable(tmp_path) -> None:
    """Se guarda como JSON en SQLite y se sirve por la API; si no serializa, no existe."""

    resumen, _, servicio, _ = await llamada_completa(tmp_path)
    try:
        texto = json.dumps(resumen, ensure_ascii=False)
        assert json.loads(texto) == resumen
        assert len(texto) > 500, "un resumen de 500 caracteres no contiene seis elementos"
    finally:
        servicio.cerrar()
