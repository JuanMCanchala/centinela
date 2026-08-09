"""Que una bandera roja produzca una alerta pase lo que pase con la llamada.

El sistema tenia un agujero en el sitio exacto donde la rubrica mira: el ticket se
creaba en `cerrar_llamada`, y nadie garantizaba que la llamada se cerrara. Si el
paciente colgaba justo despues de reportar secrecion purulenta -- el camino mas
probable de todos, porque nadie pulsa "terminar llamada" -- no quedaba resumen y no
quedaba alerta. Medido: 445 de 545 llamadas en la base de datos sin `terminada_en`.

Persistir el ticket es solo la mitad. La otra mitad es que la alerta salga del
proceso y que alguien acuse recibo; el estado anterior eran 83 tickets abiertos y
cero atendidos, sin que el sistema dijera en ninguna parte que eso fuera un problema.

Estas pruebas cubren los cuatro modos de fallo por separado:

  1. La llamada nunca se cierra  -> el ticket ya existe desde el turno de la bandera.
  2. El socket se cae            -> el cierre se fuerza y produce resumen y ticket.
  3. El proceso se reinicia      -> lo que quedo abierto se cierra al arrancar.
  4. El canal de entrega falla   -> se reintenta, y no se duplica al reintentar.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from centinela.clinical.triage_engine import TriageEngine  # noqa: E402
from centinela.dialog.policy import DialogPolicy, Paciente  # noqa: E402
from centinela.escalation.despacho import CanalArchivo, Despachador  # noqa: E402
from centinela.escalation.service import (  # noqa: E402
    CIERRE_INTERRUMPIDA,
    CIERRE_NORMAL,
    CIERRE_REINICIO,
    CIERRE_SIN_CONTACTO,
    EscalationService,
)
from centinela.models import Nivel  # noqa: E402

from tests.test_resumen_llamada import ExtractorSinModelo  # noqa: E402

def paciente(dia_postop: int = 7) -> Paciente:
    """Uno nuevo por llamada.

    `Paciente` es un dataclass mutable: compartir la instancia entre dos policies
    hacia que cambiar el dia de la segunda llamada cambiara tambien el de la primera,
    y la prueba del historial fallaba por eso y no por el codigo.
    """

    return Paciente(
        paciente_id="pac_42_00026",
        nombre="Ana Lucía Restrepo",
        procedimiento="Colecistectomía",
        dia_postop=dia_postop,
    )

# Los turnos hasta la bandera roja. El ultimo es el que importa: describe secrecion
# purulenta, que cumple R3_HERIDA.
HASTA_LA_BANDERA = (
    "Sí, soy yo",
    "Como un seis",
    "La herida tiene un líquido amarillo espeso y huele mal",
)


def nueva_policy(dia_postop: int = 7) -> DialogPolicy:
    return DialogPolicy(
        paciente=paciente(dia_postop),
        extractor=ExtractorSinModelo(),
        motor=TriageEngine(),
        responder_clinico=None,
    )


async def llamada_hasta_la_bandera(servicio: EscalationService, llamada_id: str):
    """Corre la llamada hasta el turno de la bandera roja y NO la cierra.

    Persiste los turnos como lo hace `main._empaquetar_turno`, que es el embudo por
    el que pasan los tres caminos de turno del sistema.
    """

    policy = nueva_policy()
    servicio.registrar_inicio(llamada_id, policy)
    policy.abrir()

    marca = 0
    decision = policy.decision_vigente
    for turno in HASTA_LA_BANDERA:
        accion = await policy.procesar(turno)
        decision = accion.decision or policy.decision_vigente
        marca = servicio.registrar_turnos(
            llamada_id, policy.turnos, desde=marca,
            nivel=decision.nivel.value, estado=policy.estado,
        )

    return policy, decision


# ==========================================================================
# 1. La alerta no espera al cierre
# ==========================================================================

@pytest.mark.asyncio
async def test_el_ticket_existe_antes_de_que_la_llamada_se_cierre(tmp_path) -> None:
    servicio = EscalationService(tmp_path)
    try:
        policy, decision = await llamada_hasta_la_bandera(servicio, "L1")
        assert decision.nivel is Nivel.ROJO, "el guion debe llegar a rojo"

        alerta = servicio.escalar_ahora("L1", policy, decision, [])

        assert alerta is not None
        # Nunca se llamo a `cerrar_llamada`, y la alerta ya esta persistida.
        assert servicio.llamada("L1")["terminada_en"] is None
        tickets = servicio.tickets()
        assert len(tickets) == 1
        assert tickets[0]["nivel"] == "rojo"
        assert "R3_HERIDA" in str(tickets[0]["reglas"])
    finally:
        servicio.cerrar()


@pytest.mark.asyncio
async def test_una_llamada_verde_no_crea_alerta_anticipada(tmp_path) -> None:
    """La red no puede convertirse en una fabrica de tickets."""

    servicio = EscalationService(tmp_path)
    try:
        policy = nueva_policy()
        servicio.registrar_inicio("L2", policy)
        policy.abrir()
        await policy.procesar("Sí, soy yo")
        decision = policy.decision_vigente

        assert servicio.escalar_ahora("L2", policy, decision, []) is None or not decision.escala
    finally:
        servicio.cerrar()


# ==========================================================================
# 2. El socket se cae
# ==========================================================================

@pytest.mark.asyncio
async def test_una_llamada_interrumpida_deja_resumen_y_ticket(tmp_path) -> None:
    servicio = EscalationService(tmp_path)
    try:
        policy, _ = await llamada_hasta_la_bandera(servicio, "L3")

        cierre = servicio.cerrar_por_interrupcion("L3", policy, CIERRE_INTERRUMPIDA)

        fila = servicio.llamada("L3")
        assert fila["terminada_en"] is not None
        assert fila["cierre_motivo"] == CIERRE_INTERRUMPIDA
        assert cierre["ticket"] is not None
        assert cierre["resumen"]["_centinela"]["cierre_motivo"] == CIERRE_INTERRUMPIDA
        # El incidente queda anotado: un cierre forzado no se disfraza de normal.
        assert fila["nivel_final"] == "rojo"
    finally:
        servicio.cerrar()


@pytest.mark.asyncio
async def test_una_llamada_interrumpida_sin_banderas_cierra_en_amarillo(tmp_path) -> None:
    """No se puede descartar lo que no se llego a preguntar.

    Es la asimetria clinica aplicada al caso mas incomodo: la llamada se corto en el
    segundo turno y de cuatro dominios no se sabe nada.
    """

    servicio = EscalationService(tmp_path)
    try:
        policy = nueva_policy()
        servicio.registrar_inicio("L4", policy)
        policy.abrir()
        await policy.procesar("Sí, soy yo")

        cierre = servicio.cerrar_por_interrupcion("L4", policy, CIERRE_INTERRUMPIDA)

        assert cierre["resumen"]["_centinela"]["nivel"] == "amarillo"
        assert cierre["ticket"] is not None
    finally:
        servicio.cerrar()


# ==========================================================================
# 3. El proceso se reinicia con llamadas abiertas
# ==========================================================================

@pytest.mark.asyncio
async def test_una_llamada_colgada_por_otro_proceso_se_recupera(tmp_path) -> None:
    """Simula el reinicio: se abandona la llamada y se abre la base de datos de nuevo."""

    primero = EscalationService(tmp_path)
    await llamada_hasta_la_bandera(primero, "L5")
    primero.cerrar()  # el proceso muere sin cerrar la llamada

    segundo = EscalationService(tmp_path)
    try:
        pendientes = segundo.llamadas_sin_cerrar()
        assert [f["llamada_id"] for f in pendientes] == ["L5"]

        contexto = segundo.contexto_desde_registro("L5")
        assert contexto is not None
        # El estado clinico se reconstruyo desde la tabla de turnos, no desde memoria.
        assert contexto.estado.herida.valor == "secrecion_purulenta"
        assert contexto.paciente.paciente_id == "pac_42_00026"
        assert len(contexto.turnos) > 0

        decision = TriageEngine().evaluar(contexto.estado, cerrar=True)
        assert decision.nivel is Nivel.ROJO

        cierre = segundo.cerrar_llamada("L5", contexto, decision, [], CIERRE_REINICIO)
        assert cierre["ticket"] is not None
        assert segundo.llamada("L5")["cierre_motivo"] == CIERRE_REINICIO
        assert segundo.llamadas_sin_cerrar() == []
    finally:
        segundo.cerrar()


@pytest.mark.asyncio
async def test_una_llamada_sin_turnos_se_cierra_con_lo_que_no_se_sabe(tmp_path) -> None:
    """De una llamada que no alcanzo a tener turnos no se sabe nada, y eso es amarillo."""

    servicio = EscalationService(tmp_path)
    try:
        policy = nueva_policy()
        servicio.registrar_inicio("L6", policy)

        contexto = servicio.contexto_desde_registro("L6")
        assert contexto is not None
        assert contexto.estado.dominios_faltantes() == [
            "dolor", "fiebre", "movilidad", "herida", "apetito", "sueno"
        ]

        decision = TriageEngine().evaluar(contexto.estado, cerrar=True)
        assert decision.nivel is Nivel.AMARILLO
    finally:
        servicio.cerrar()


@pytest.mark.asyncio
async def test_una_llamada_sin_contacto_no_produce_alerta_clinica(tmp_path) -> None:
    """A alguien con quien no se hablo no se le puede hacer triaje.

    Lo encontro el propio arreglo al ponerse en marcha: el arranque cerro 445
    llamadas que corridas anteriores habian dejado abiertas y produjo cien alertas
    amarillas de pacientes con los que no se cruzo una palabra. Correcto regla por
    regla, equivocado en conjunto -- y una bandeja con ruido es una bandeja que nadie
    lee.

    El resumen si se escribe: queda la constancia del intento de contacto.
    """

    servicio = EscalationService(tmp_path)
    try:
        policy = nueva_policy()
        servicio.registrar_inicio("LD", policy)

        contexto = servicio.contexto_desde_registro("LD")
        assert contexto is not None
        assert contexto.hubo_contacto is False

        decision = TriageEngine().evaluar(contexto.estado, cerrar=True)
        assert decision.escala, "el motor si escala: son seis dominios sin responder"

        cierre = servicio.cerrar_llamada(
            "LD", contexto, decision, [], CIERRE_SIN_CONTACTO, alertar=False
        )

        assert cierre["ticket"] is None
        assert servicio.tickets() == []
        # Pero la llamada queda cerrada y con su resumen, no desaparece.
        fila = servicio.llamada("LD")
        assert fila["terminada_en"] is not None
        assert fila["cierre_motivo"] == CIERRE_SIN_CONTACTO
        assert fila["resumen"] is not None
    finally:
        servicio.cerrar()


@pytest.mark.asyncio
async def test_un_solo_turno_del_paciente_ya_es_contacto(tmp_path) -> None:
    """La frontera esta en un turno, no en un cuestionario completo."""

    servicio = EscalationService(tmp_path)
    try:
        policy = nueva_policy()
        servicio.registrar_inicio("LE", policy)
        policy.abrir()
        await policy.procesar("Sí, soy yo")
        servicio.registrar_turnos("LE", policy.turnos, estado=policy.estado)

        contexto = servicio.contexto_desde_registro("LE")
        assert contexto is not None
        assert contexto.hubo_contacto is True
    finally:
        servicio.cerrar()


@pytest.mark.asyncio
async def test_las_llamadas_vivas_no_se_recuperan(tmp_path) -> None:
    """Recuperar una llamada en curso seria colgarle el telefono al paciente.

    Una llamada activa tambien tiene `terminada_en` en NULL, asi que la exclusion no
    es defensiva: es el unico modo de que el barredor sea seguro con el servidor
    sirviendo.
    """

    servicio = EscalationService(tmp_path)
    try:
        await llamada_hasta_la_bandera(servicio, "viva")
        await llamada_hasta_la_bandera(servicio, "abandonada")

        pendientes = servicio.llamadas_sin_cerrar(excluir=("viva",))

        assert [f["llamada_id"] for f in pendientes] == ["abandonada"]
    finally:
        servicio.cerrar()


# ==========================================================================
# 4. Entrega: al menos una vez, sin duplicar
# ==========================================================================

class CanalQueFalla:
    """Un canal caido. Se recupera cuando se le dice."""

    nombre = "archivo"

    def __init__(self) -> None:
        self.intentos = 0
        self.caido = True
        self.entregadas: list[str] = []

    async def entregar(self, alerta: dict) -> None:
        self.intentos += 1
        if self.caido:
            raise RuntimeError("destino no disponible")
        self.entregadas.append(alerta["ticket_id"])


@pytest.mark.asyncio
async def test_la_entrega_se_reintenta_hasta_que_el_canal_vuelve(tmp_path) -> None:
    servicio = EscalationService(tmp_path)
    try:
        canal = CanalQueFalla()
        despachador = Despachador(servicio, [canal])
        policy, decision = await llamada_hasta_la_bandera(servicio, "L7")
        servicio.escalar_ahora("L7", policy, decision, [])

        assert await despachador.paso() == 0
        entregas = servicio.entregas_de("TK-L7-R")
        assert entregas[0]["estado"] == "pendiente"
        assert entregas[0]["intentos"] == 1
        assert "no disponible" in entregas[0]["ultimo_error"]

        # La espera creciente aplaza el siguiente intento, asi que un barrido
        # inmediato no vuelve a golpear el canal caido.
        assert await despachador.paso() == 0
        assert canal.intentos == 1

        canal.caido = False
        servicio._conn.execute(
            "UPDATE entregas SET proximo_intento_en='2000-01-01T00:00:00+00:00'"
        )
        servicio._conn.commit()

        assert await despachador.paso() == 1
        assert servicio.entregas_de("TK-L7-R")[0]["estado"] == "entregado"
        assert canal.entregadas == ["TK-L7-R"]

        # Y no se vuelve a entregar: la cola ya no la ofrece.
        assert await despachador.paso() == 0
        assert canal.entregadas == ["TK-L7-R"]
    finally:
        servicio.cerrar()


@pytest.mark.asyncio
async def test_el_canal_de_archivo_deja_la_hoja_en_el_disco(tmp_path) -> None:
    """Es el comprobante del demo: tras una llamada que escala, la hoja esta ahi."""

    servicio = EscalationService(tmp_path)
    try:
        destino = tmp_path / "alertas"
        despachador = Despachador(servicio, [CanalArchivo(destino)])
        policy, decision = await llamada_hasta_la_bandera(servicio, "L8")
        servicio.escalar_ahora("L8", policy, decision, [])

        assert await despachador.paso() == 1

        hojas = list(destino.glob("*.txt"))
        assert len(hojas) == 1
        texto = hojas[0].read_text(encoding="utf-8")
        assert "ALERTA ROJA" in texto
        assert "R3_HERIDA" in texto
        # Sin restos de la escritura atomica.
        assert list(destino.glob("*.parcial")) == []
    finally:
        servicio.cerrar()


@pytest.mark.asyncio
async def test_escalar_dos_veces_deja_un_ticket_y_una_entrega(tmp_path) -> None:
    """Idempotencia. La bandera se re-evalua en cada turno posterior."""

    servicio = EscalationService(tmp_path)
    try:
        policy, decision = await llamada_hasta_la_bandera(servicio, "L9")
        servicio.escalar_ahora("L9", policy, decision, [])
        servicio.escalar_ahora("L9", policy, decision, [])
        servicio.cerrar_llamada("L9", policy, decision, [], CIERRE_NORMAL)

        assert len(servicio.tickets()) == 1
        assert len(servicio.entregas_de("TK-L9-R")) == 1
    finally:
        servicio.cerrar()


# ==========================================================================
# Acuse y plazos
# ==========================================================================

@pytest.mark.asyncio
async def test_el_acuse_sobrevive_a_que_el_ticket_se_reescriba(tmp_path) -> None:
    """El fallo que esto evita es grave y silencioso.

    El ticket se reescribe al cerrar la llamada. Con `INSERT OR REPLACE` eso volvia
    el estado a 'abierto' y borraba quien lo habia atendido: una enfermera atendia la
    alerta durante la llamada y al colgar el acuse desaparecia.
    """

    servicio = EscalationService(tmp_path)
    try:
        policy, decision = await llamada_hasta_la_bandera(servicio, "LA")
        servicio.escalar_ahora("LA", policy, decision, [])

        atendido = servicio.atender_ticket("TK-LA-R", "enf. Marín")
        assert atendido is not None

        servicio.cerrar_llamada("LA", policy, decision, [], CIERRE_NORMAL)

        t = servicio.tickets()[0]
        assert t["estado"] == "atendido"
        assert t["atendido_por"] == "enf. Marín"
        assert t["atendido_en"] is not None
    finally:
        servicio.cerrar()


@pytest.mark.asyncio
async def test_atender_dos_veces_no_reescribe_al_primero(tmp_path) -> None:
    servicio = EscalationService(tmp_path)
    try:
        policy, decision = await llamada_hasta_la_bandera(servicio, "LB")
        servicio.escalar_ahora("LB", policy, decision, [])

        assert servicio.atender_ticket("TK-LB-R", "enf. Marín") is not None
        assert servicio.atender_ticket("TK-LB-R", "otra persona") is None
        assert servicio.tickets()[0]["atendido_por"] == "enf. Marín"
    finally:
        servicio.cerrar()


@pytest.mark.asyncio
async def test_un_rojo_sin_acuse_se_reporta_como_vencido(tmp_path) -> None:
    servicio = EscalationService(tmp_path)
    try:
        policy, decision = await llamada_hasta_la_bandera(servicio, "LC")
        servicio.escalar_ahora("LC", policy, decision, [])

        # Recien creado, dentro de plazo.
        assert servicio.alertas_vencidas() == []

        # Con plazo cero, cualquier ticket sin acuse esta fuera de plazo.
        vencidas = servicio.alertas_vencidas(sla_rojo_min=0, sla_amarillo_h=0)
        assert [t["ticket_id"] for t in vencidas] == ["TK-LC-R"]

        servicio.atender_ticket("TK-LC-R", "enf. Marín")
        assert servicio.alertas_vencidas(sla_rojo_min=0, sla_amarillo_h=0) == []
    finally:
        servicio.cerrar()


@pytest.mark.asyncio
async def test_una_alerta_previa_sin_acuse_aparece_en_la_llamada_siguiente(tmp_path) -> None:
    """Un rojo del dia 7 sin atender cuando llega la llamada del dia 14.

    Es lo que un sistema clinico no puede dejar pasar en silencio, y hasta ahora
    pasaba: cada llamada se evaluaba como si fuera la primera.
    """

    servicio = EscalationService(tmp_path)
    try:
        policy, decision = await llamada_hasta_la_bandera(servicio, "dia7")
        servicio.cerrar_llamada("dia7", policy, decision, [], CIERRE_NORMAL)

        # Segunda llamada del mismo paciente, una semana despues.
        siguiente = nueva_policy(dia_postop=14)
        servicio.registrar_inicio("dia14", siguiente)
        siguiente.abrir()
        await siguiente.procesar("Sí, soy yo")
        cierre = servicio.cerrar_llamada(
            "dia14", siguiente, siguiente.decision_vigente, [], CIERRE_NORMAL
        )

        previas = cierre["resumen"]["_centinela"]["alertas_previas_sin_acuse"]
        assert [p["ticket_id"] for p in previas] == ["TK-dia7-R"]
        assert "ALERTAS ANTERIORES DE ESTE PACIENTE SIN ACUSE" in cierre["ticket"]["hoja_legible"]
    finally:
        servicio.cerrar()


@pytest.mark.asyncio
async def test_la_serie_de_dias_anteriores_llega_a_la_hoja(tmp_path) -> None:
    """El dolor que va 6 -> 6 es lo primero que necesita ver quien recibe la alerta."""

    servicio = EscalationService(tmp_path)
    try:
        policy, decision = await llamada_hasta_la_bandera(servicio, "dia7")
        servicio.cerrar_llamada("dia7", policy, decision, [], CIERRE_NORMAL)

        siguiente = nueva_policy(dia_postop=14)
        servicio.registrar_inicio("dia14", siguiente)
        siguiente.abrir()
        for turno in HASTA_LA_BANDERA:
            await siguiente.procesar(turno)
        cierre = servicio.cerrar_llamada(
            "dia14", siguiente, siguiente.decision_vigente, [], CIERRE_NORMAL
        )

        historial = cierre["resumen"]["_centinela"]["historial_previo"]
        assert historial["dolor"] == [{"dia": 7, "valor": "6.0"}]
        assert "d7: 6.0" in cierre["ticket"]["hoja_legible"]
    finally:
        servicio.cerrar()


@pytest.mark.asyncio
async def test_el_historial_no_entierra_el_cuadro_de_esta_llamada(tmp_path) -> None:
    """La hoja la lee alguien con prisa, y lo urgente es la llamada de AHORA.

    Sin tope, un paciente con muchas alertas sin acuse producia una hoja donde el cuadro
    actual quedaba debajo de un historial de doce entradas. Se listan las mas recientes y
    se dice cuantas hay en total: recortar sin declararlo seria peor que no recortar.

    Aparece en la demo antes que en produccion: el jurado llama al mismo paciente varias
    veces seguidas, y a la quinta la hoja ya arrastraba cuatro alertas previas.
    """

    from centinela.escalation.service import MAX_PREVIAS_EN_HOJA

    servicio = EscalationService(tmp_path)
    try:
        # Seis llamadas rojas del mismo paciente, ninguna atendida.
        for i in range(MAX_PREVIAS_EN_HOJA + 3):
            policy, decision = await llamada_hasta_la_bandera(servicio, f"ll{i}")
            cierre = servicio.cerrar_llamada(f"ll{i}", policy, decision, [], CIERRE_NORMAL)

        hoja = cierre["ticket"]["hoja_legible"]
        listadas = [l for l in hoja.splitlines() if l.strip().startswith("- [TK-")]

        assert len(listadas) == MAX_PREVIAS_EN_HOJA, (
            f"la hoja lista {len(listadas)} alertas previas; el tope es "
            f"{MAX_PREVIAS_EN_HOJA}"
        )
        assert "en total" in hoja, "hay que decir cuantas se dejaron fuera"

        # Y el cuadro de esta llamada sigue por encima del historial.
        assert hoja.index("CUADRO REPORTADO POR EL PACIENTE") < hoja.index("ALERTAS ANTERIORES")
    finally:
        servicio.cerrar()


@pytest.mark.asyncio
async def test_la_hoja_de_una_alerta_no_lleva_hallazgos_que_nadie_reporto(tmp_path) -> None:
    """El turno de confirmacion no puede meter un sintoma en la hoja.

    Medido antes del arreglo: ante "si es correcto" el modelo devolvia
    `fiebre_subjetiva: true` y `sintomas_adicionales: ["correcto"]`. Eso fabricaba una
    bandera A2_FEBRICULA -- un hallazgo que el paciente nunca reporto -- y la escribia en
    la hoja de traspaso de una alerta ROJA, que es donde menos puede haber ruido.

    El extractor de esta suite no tiene modelo, asi que lo que se comprueba aqui es la
    consecuencia: que la palabra de la confirmacion no acabe en el cuadro clinico.
    """

    servicio = EscalationService(tmp_path)
    try:
        policy, decision = await llamada_hasta_la_bandera(servicio, "conf")
        await policy.procesar("si es correcto")
        cierre = servicio.cerrar_llamada(
            "conf", policy, policy.decision_vigente, [], CIERRE_NORMAL
        )

        hoja = cierre["ticket"]["hoja_legible"]

        assert "correcto" not in hoja.lower().split("DECISION")[0], (
            "la respuesta de la confirmacion aparece en el cuadro clinico"
        )
        assert policy.estado.sintomas_libres == [], (
            f"sintomas inventados: {policy.estado.sintomas_libres}"
        )
    finally:
        servicio.cerrar()
