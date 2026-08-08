"""Escalamiento y resumen estructurado de la llamada.

La rubrica pregunta literalmente: "que produce el sistema cuando decide alertar:
que queda registrado, con que estructura y con que persistencia, y que se le
comunica al paciente sobre el siguiente paso". Y sobre el cierre: "si existe un
resumen que identifique al paciente y su procedimiento, los sintomas reportados,
la decision tomada, las referencias usadas y los proximos pasos".

Este modulo produce las tres cosas:

1. **Ticket persistente** en SQLite con WAL. Sobrevive al reinicio del proceso.
   Incluye la version de las reglas que tomaron la decision, para que un ticket
   de hace un mes siga siendo interpretable despues de cambiar los umbrales.

2. **Resumen con forma FHIR** (Encounter / Observation / CommunicationRequest).
   No es FHIR certificado y no lo presentamos como tal: es la forma de los
   recursos FHIR, para que integrar esto con un HIS real sea un mapeo y no una
   reescritura.

3. **Hoja legible** para el humano que recibe la alerta. Un JSON no se lee a las
   tres de la manana.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..dialog.policy import DialogPolicy, Paciente, TurnoRegistrado
from ..models import ClinicalState, Nivel, TriageDecision

ESQUEMA = """
CREATE TABLE IF NOT EXISTS llamadas (
    llamada_id      TEXT PRIMARY KEY,
    paciente_id     TEXT NOT NULL,
    nombre          TEXT NOT NULL,
    procedimiento   TEXT NOT NULL,
    dia_postop      INTEGER NOT NULL,
    iniciada_en     TEXT NOT NULL,
    terminada_en    TEXT,
    nivel_final     TEXT,
    version_reglas  TEXT,
    n_turnos        INTEGER NOT NULL DEFAULT 0,
    resumen_json    TEXT,
    transcripcion   TEXT
);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id       TEXT PRIMARY KEY,
    llamada_id      TEXT NOT NULL REFERENCES llamadas(llamada_id),
    creado_en       TEXT NOT NULL,
    nivel           TEXT NOT NULL,
    estado          TEXT NOT NULL DEFAULT 'abierto',
    motivo          TEXT NOT NULL,
    reglas_json     TEXT NOT NULL,
    citas_json      TEXT NOT NULL,
    version_reglas  TEXT NOT NULL,
    hoja_legible    TEXT NOT NULL,
    atendido_por    TEXT,
    atendido_en     TEXT
);
CREATE INDEX IF NOT EXISTS idx_tickets_estado ON tickets(estado, nivel);

CREATE TABLE IF NOT EXISTS incidentes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    llamada_id  TEXT NOT NULL,
    momento     TEXT NOT NULL,
    tipo        TEXT NOT NULL,
    detalle     TEXT NOT NULL
);

-- Registro por turno. Es lo unico que sobrevive a una caida en mitad de la
-- llamada, y por tanto la fuente con la que se reconstruye un cierre que nunca
-- llego a ocurrir. `estado_json` guarda el ClinicalState completo tal como
-- estaba tras ese turno.
CREATE TABLE IF NOT EXISTS turnos (
    llamada_id  TEXT NOT NULL,
    turno_idx   INTEGER NOT NULL,
    hablante    TEXT NOT NULL,
    momento     TEXT NOT NULL,
    texto       TEXT NOT NULL,
    nivel       TEXT,
    estado_json TEXT,
    PRIMARY KEY (llamada_id, turno_idx, hablante)
);

-- Outbox de entrega. Un ticket en una tabla no es una alerta: la alerta tiene
-- que salir del proceso. Cada fila es el intento de entregar un ticket por un
-- canal, con su reintento pendiente. `UNIQUE(ticket_id, canal)` es lo que hace
-- que reencolar sea idempotente.
CREATE TABLE IF NOT EXISTS entregas (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id          TEXT NOT NULL,
    canal              TEXT NOT NULL,
    estado             TEXT NOT NULL DEFAULT 'pendiente',
    intentos           INTEGER NOT NULL DEFAULT 0,
    creado_en          TEXT NOT NULL,
    proximo_intento_en TEXT NOT NULL,
    entregado_en       TEXT,
    ultimo_error       TEXT,
    UNIQUE (ticket_id, canal)
);
CREATE INDEX IF NOT EXISTS idx_entregas_pendientes
    ON entregas(estado, proximo_intento_en);

-- Serie por paciente. Se materializa al cerrar para no tener que leer el JSON
-- del resumen cada vez que se quiere la tendencia de un dominio.
CREATE TABLE IF NOT EXISTS mediciones (
    llamada_id  TEXT NOT NULL,
    paciente_id TEXT NOT NULL,
    dia_postop  INTEGER NOT NULL,
    dominio     TEXT NOT NULL,
    valor       TEXT,
    medido_en   TEXT NOT NULL,
    PRIMARY KEY (llamada_id, dominio)
);
CREATE INDEX IF NOT EXISTS idx_mediciones_paciente
    ON mediciones(paciente_id, dia_postop);
"""

# Columnas anadidas despues del esquema original. `executescript` con
# `CREATE TABLE IF NOT EXISTS` no toca una tabla que ya existe, asi que una base
# de datos de una version anterior se queda sin ellas si no se migra a mano.
COLUMNAS_NUEVAS = (
    ("llamadas", "cierre_motivo", "TEXT"),
    ("llamadas", "ultimo_turno_en", "TEXT"),
)

# Como termino la llamada. Importa porque distingue un cierre normal de uno que
# el sistema tuvo que forzar, y eso cambia como se lee el resumen.
CIERRE_NORMAL = "normal"
CIERRE_INTERRUMPIDA = "interrumpida"
CIERRE_TIMEOUT = "timeout"
CIERRE_REINICIO = "reinicio"

# La llamada se abrio y el paciente no dijo nada: ni un turno. No es un hallazgo
# clinico, es un intento de contacto fallido, y la distincion importa. A alguien con
# quien no se hablo no se le puede hacer triaje, asi que meter esa llamada en la
# bandeja de alertas clinicas como AMARILLO es ruido con forma de hallazgo -- y una
# bandeja con ruido es una bandeja que nadie lee. Lo que hay que hacer con estas es
# volver a llamar, que es una tarea de operacion.
CIERRE_SIN_CONTACTO = "sin_contacto"

PRIORIDAD_FHIR = {"rojo": "urgent", "amarillo": "routine", "verde": "routine"}

DOMINIOS_RESUMEN = ("dolor", "fiebre", "movilidad", "herida", "apetito", "sueno")

# Reintento del despachador: 30 s, 1 min, 2 min... con tope. La progresion importa
# poco; lo que importa es que no se rinda y que no golpee un webhook caido.
ESPERA_BASE_S = 30
ESPERA_MAXIMA_S = 900
MAX_INTENTOS = 12

# Plazos de acuse. Un rojo sin atender es lo unico que no puede quedarse quieto en
# una bandeja: por eso se mide en minutos y no en horas.
SLA_ROJO_MINUTOS = 15
SLA_AMARILLO_HORAS = 24

CODIGOS_LOINC = {
    "dolor_nrs": ("72514-3", "Pain severity - 0-10 verbal numeric rating"),
    "fiebre_c": ("8310-5", "Body temperature"),
    "movilidad": ("28551-1", "Mobility status"),
    "herida": ("72169-6", "Surgical wound assessment"),
    "apetito": ("64146-2", "Appetite"),
    "sueno": ("65554-6", "Sleep quality"),
}


@dataclass
class Ticket:
    ticket_id: str
    llamada_id: str
    nivel: str
    motivo: str
    hoja_legible: str
    creado_en: str


@dataclass
class ContextoCierre:
    """Lo que hace falta para escribir el resumen de una llamada.

    Existe porque `construir_resumen` y `hoja_legible` recibian un `DialogPolicy`
    completo, y tras un reinicio del proceso ese objeto ya no existe: lo unico que
    queda es lo que se escribio en la tabla `turnos`. Con este contrato explicito,
    la recuperacion puede armar el mismo resumen desde la base de datos sin
    duplicar una linea de la logica del cierre.

    `DialogPolicy` lo satisface por estructura, asi que el camino normal no cambia
    y no hay dos formas de cerrar una llamada.
    """

    paciente: Paciente
    estado: ClinicalState
    turnos: list[TurnoRegistrado]
    iniciada_en: datetime
    preguntas_sin_responder: list[str] = field(default_factory=list)
    incidentes: list[str] = field(default_factory=list)
    consultas_rag: int = 0
    # Lo que se le leyo de vuelta al paciente y como respondio. Ver
    # `dialog/confirmacion.py`.
    confirmaciones: list[dict] = field(default_factory=list)
    # False cuando no hay ni un turno del paciente. `DialogPolicy` no tiene este
    # atributo y no le hace falta: si hay una policy viva, la llamada ocurrio.
    hubo_contacto: bool = True
    # Si el paciente oyo entera la instruccion de irse a urgencias. `None` cuando no
    # hubo ninguna, que es el caso de toda llamada que no escala.
    urgencia_oida: bool | None = None


class EscalationService:
    def __init__(self, ruta_datos: Path) -> None:
        self.ruta = Path(ruta_datos)
        self.ruta.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.ruta / "llamadas.db", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # Sin esto, dos escrituras concurrentes (un turno y el despachador de
        # alertas) pueden cruzarse y una recibe "database is locked" al instante en
        # vez de esperar su turno.
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(ESQUEMA)
        self._migrar()
        self._conn.commit()

    def _migrar(self) -> None:
        """Anade las columnas que no estaban en el esquema original."""

        for tabla, columna, tipo in COLUMNAS_NUEVAS:
            filas = self._conn.execute(f"PRAGMA table_info({tabla})").fetchall()
            existentes = {f["name"] for f in filas}
            if columna not in existentes:
                self._conn.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {tipo}")

    # ------------------------------------------------------------------

    def registrar_inicio(self, llamada_id: str, policy: DialogPolicy) -> None:
        p = policy.paciente
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO llamadas(llamada_id, paciente_id, nombre,"
                " procedimiento, dia_postop, iniciada_en) VALUES(?,?,?,?,?,?)",
                (
                    llamada_id, p.paciente_id, p.nombre, p.procedimiento,
                    p.dia_postop, policy.iniciada_en.isoformat(),
                ),
            )
            self._conn.commit()

    def registrar_turnos(
        self,
        llamada_id: str,
        turnos: list[TurnoRegistrado],
        desde: int = 0,
        nivel: str | None = None,
        estado: ClinicalState | None = None,
    ) -> int:
        """Escribe los turnos nuevos y devuelve cuantos hay ya escritos.

        Lo que compra es que una llamada cortada a mitad deje rastro: sin esto, lo
        que el paciente dijo en el turno 4 solo existia en memoria del proceso, y con
        el proceso se iba.

        El valor devuelto es la marca de agua para la proxima llamada. Reescribir la
        lista completa en cada turno seria correcto -- el INSERT es `OR REPLACE` --
        pero cada commit es un fsync, y en una llamada de veinte turnos eso son
        cientos de fsync en el camino que la rubrica cronometra. Se escribe solo lo
        nuevo, en una sola transaccion.

        El estado clinico se guarda pegado al ultimo turno: es el que la recuperacion
        lee para reconstruir el cierre.
        """

        ahora = datetime.now(timezone.utc).isoformat()
        nuevos = turnos[desde:]

        with self._lock:
            for i, t in enumerate(nuevos):
                ultimo = i == len(nuevos) - 1
                self._conn.execute(
                    "INSERT OR REPLACE INTO turnos(llamada_id, turno_idx, hablante,"
                    " momento, texto, nivel, estado_json) VALUES(?,?,?,?,?,?,?)",
                    (
                        llamada_id, t.turno_idx, t.hablante, t.momento, t.texto,
                        nivel if ultimo else None,
                        estado.model_dump_json() if (ultimo and estado is not None) else None,
                    ),
                )
            if nuevos:
                self._conn.execute(
                    "UPDATE llamadas SET ultimo_turno_en=?, n_turnos=? WHERE llamada_id=?",
                    (ahora, len(turnos), llamada_id),
                )
            self._conn.commit()

        return len(turnos)

    def reescribir_turno(self, llamada_id: str, turno_idx: int, texto: str) -> bool:
        """Corrige el texto de un turno ya persistido.

        Existe por una sola razon, y es clinica: cuando el paciente interrumpe al
        agente, el turno del agente ya se escribio con lo que se PLANEABA decir. La
        politica lo recorta en memoria a lo que el paciente de verdad oyo, y sin esto
        esa correccion no llegaba al registro durable -- la hoja que lee una enfermera
        seguiria afirmando una pregunta que se corto en la primera silaba.

        Es un UPDATE y no un INSERT OR REPLACE: no puede crear una fila. Si el turno no
        estaba escrito todavia, la escritura normal ya llevara el texto corregido.
        """

        with self._lock:
            cur = self._conn.execute(
                "UPDATE turnos SET texto=? WHERE llamada_id=? AND turno_idx=?",
                (texto, llamada_id, turno_idx),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def registrar_incidente(self, llamada_id: str, tipo: str, detalle: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO incidentes(llamada_id, momento, tipo, detalle) VALUES(?,?,?,?)",
                (llamada_id, datetime.now(timezone.utc).isoformat(), tipo, detalle),
            )
            self._conn.commit()

    # ------------------------------------------------------------------

    def cerrar_llamada(
        self,
        llamada_id: str,
        policy: ContextoCierre | DialogPolicy,
        decision: TriageDecision,
        citas: list[dict],
        motivo: str = CIERRE_NORMAL,
        alertar: bool = True,
    ) -> dict:
        """Persiste el resumen y, si corresponde, crea el ticket de alerta.

        `motivo` distingue el cierre normal del que el sistema tuvo que forzar
        porque nadie lo cerro -- se colgo la llamada, expiro la inactividad, o el
        proceso se reinicio con la llamada abierta. En los tres casos el resumen y
        el ticket se producen igual: una bandera roja detectada no depende de que
        la llamada termine bien.

        `alertar=False` es para la llamada en la que el paciente nunca hablo. El
        resumen se escribe -- queda constancia del intento -- pero no se crea alerta
        clinica, porque no hay nada que triar. Ver `CIERRE_SIN_CONTACTO`.
        """

        resumen = self.construir_resumen(llamada_id, policy, decision, citas)
        resumen["_centinela"]["cierre_motivo"] = motivo
        transcripcion = self._transcribir(policy)
        ahora = datetime.now(timezone.utc).isoformat()

        with self._lock:
            self._conn.execute(
                "UPDATE llamadas SET terminada_en=?, nivel_final=?, version_reglas=?,"
                " n_turnos=?, resumen_json=?, transcripcion=?, cierre_motivo=?"
                " WHERE llamada_id=?",
                (
                    ahora, decision.nivel.value, decision.version_reglas,
                    len(policy.turnos),
                    json.dumps(resumen, indent=2, ensure_ascii=False),
                    transcripcion, motivo, llamada_id,
                ),
            )

            self._guardar_mediciones(llamada_id, policy, ahora)

            ticket = None
            if decision.escala and alertar:
                ticket = self._crear_ticket(llamada_id, policy, decision, citas, resumen)

            for inc in policy.incidentes:
                self._conn.execute(
                    "INSERT INTO incidentes(llamada_id, momento, tipo, detalle) VALUES(?,?,?,?)",
                    (llamada_id, ahora, "seguridad", inc),
                )

            self._conn.commit()

        return {"resumen": resumen, "ticket": ticket}

    def escalar_ahora(
        self,
        llamada_id: str,
        policy: ContextoCierre | DialogPolicy,
        decision: TriageDecision,
        citas: list[dict],
    ) -> dict | None:
        """Crea el ticket en el turno en que aparece la bandera, no al cerrar.

        Es el arreglo del agujero central del escalamiento. El ticket se creaba
        solo en `cerrar_llamada`, asi que una llamada que se cortaba justo despues
        de que el paciente reportara secrecion purulenta no producia NADA: ni
        resumen ni alerta. El camino mas probable de todos -- el paciente cuelga --
        era el que se perdia el dato.

        Idempotente: el `ticket_id` se deriva de la llamada y del nivel, y el ticket
        se refresca al cerrar sin perder el acuse, asi que crearlo aqui y volver a
        escribirlo despues deja una sola alerta con la informacion mas completa.

        **Solo se anticipa el ROJO**, y la razon es medida, no estetica. A mitad de
        llamada el motor devuelve AMARILLO provisional en cuanto le falta un dominio
        por preguntar -- que es el estado normal del turno 1 de cualquier llamada.
        Anticipar tambien el amarillo llenaba la bandeja con un ticket por llamada,
        y una bandeja con ruido es una bandeja que nadie lee.

        El amarillo no necesita anticiparse: significa "contacto de enfermeria en 24
        horas", y su ticket se crea al cerrar. El cierre esta garantizado por los tres
        caminos forzados (socket caido, inactividad, reinicio), asi que la alerta sale
        igual. Lo que no puede esperar es el rojo.
        """

        creado: dict | None = None
        if decision.nivel is Nivel.ROJO:
            resumen = self.construir_resumen(llamada_id, policy, decision, citas)
            with self._lock:
                creado = self._crear_ticket(llamada_id, policy, decision, citas, resumen)
                self._conn.commit()
        return creado

    def cerrar_por_interrupcion(
        self,
        llamada_id: str,
        policy: DialogPolicy,
        motivo: str,
    ) -> dict:
        """Cierra una llamada que nadie cerro, por el mismo camino que el cierre normal.

        Con dominios sin responder el motor cierra en AMARILLO
        (`triage_engine._dominios_por_indagar`), que es la conducta correcta: no se
        puede descartar lo que no se llego a preguntar.
        """

        accion = policy.cerrar_ahora()
        decision = accion.decision or policy.decision_vigente
        cierre = self.cerrar_llamada(llamada_id, policy, decision, accion.citas, motivo)
        self.registrar_incidente(
            llamada_id, "cierre_forzado",
            f"llamada cerrada por el sistema ({motivo}) tras {len(policy.turnos)} turnos",
        )
        return cierre

    # ------------------------------------------------------------------

    def llamadas_sin_cerrar(
        self, limite: int = 200, excluir: tuple[str, ...] = ()
    ) -> list[dict]:
        """Llamadas con `terminada_en` en NULL: las que nadie cerro.

        `excluir` es obligatorio en la practica cuando el proceso esta sirviendo: una
        llamada EN CURSO tambien tiene `terminada_en` en NULL, y recuperarla seria
        cerrarle la llamada al paciente mientras habla.
        """

        sql = (
            "SELECT llamada_id, paciente_id, nombre, procedimiento, dia_postop,"
            " iniciada_en, ultimo_turno_en FROM llamadas WHERE terminada_en IS NULL"
        )
        params: list[object] = []
        if excluir:
            sql += " AND llamada_id NOT IN (" + ",".join("?" * len(excluir)) + ")"
            params.extend(excluir)
        sql += " ORDER BY iniciada_en ASC LIMIT ?"
        params.append(limite)
        return [dict(f) for f in self._conn.execute(sql, params).fetchall()]

    def contexto_desde_registro(self, llamada_id: str) -> ContextoCierre | None:
        """Reconstruye lo justo para cerrar una llamada que sobrevivio a un reinicio.

        El estado clinico sale del ultimo turno que alcanzo a escribirse. Si no hay
        ningun turno con estado, se cierra con el estado vacio: seis dominios sin
        responder, que el motor traduce a AMARILLO. Es el resultado correcto -- de
        esa llamada no se sabe nada.
        """

        fila = self._conn.execute(
            "SELECT * FROM llamadas WHERE llamada_id = ?", (llamada_id,)
        ).fetchone()

        contexto: ContextoCierre | None = None
        if fila is not None:
            turnos_db = self._conn.execute(
                "SELECT turno_idx, hablante, texto, momento, estado_json FROM turnos"
                " WHERE llamada_id = ? ORDER BY turno_idx ASC, hablante ASC",
                (llamada_id,),
            ).fetchall()

            estado = ClinicalState()
            for t in turnos_db:
                if t["estado_json"]:
                    estado = ClinicalState.model_validate_json(t["estado_json"])

            # Si no hay ni un turno del paciente, la llamada no llego a ocurrir. Es
            # lo que distingue un cierre `sin_contacto` de uno interrumpido.
            hubo_paciente = any(t["hablante"] == "paciente" for t in turnos_db)

            contexto = ContextoCierre(
                hubo_contacto=hubo_paciente,
                paciente=Paciente(
                    paciente_id=fila["paciente_id"],
                    nombre=fila["nombre"],
                    procedimiento=fila["procedimiento"],
                    dia_postop=fila["dia_postop"],
                ),
                estado=estado,
                turnos=[
                    TurnoRegistrado(
                        turno_idx=t["turno_idx"],
                        hablante=t["hablante"],
                        texto=t["texto"],
                        momento=t["momento"],
                    )
                    for t in turnos_db
                ],
                iniciada_en=datetime.fromisoformat(fila["iniciada_en"]),
            )
        return contexto

    def _crear_ticket(
        self,
        llamada_id: str,
        policy: DialogPolicy,
        decision: TriageDecision,
        citas: list[dict],
        resumen: dict,
    ) -> dict:
        ticket_id = f"TK-{llamada_id[:8]}-{decision.nivel.value[:1].upper()}"
        reglas = [r.model_dump() for r in decision.reglas_rojas + decision.banderas_amarillas]
        hoja = self.hoja_legible(policy, decision, citas, llamada_id)
        creado = datetime.now(timezone.utc).isoformat()

        # `INSERT OR REPLACE` reescribiria `estado` y `atendido_por`, y eso borraria
        # el acuse de un humano que ya atendio la alerta durante la llamada. El
        # UPDATE previo conserva lo que la persona hizo y solo refresca el contenido.
        existente = self._conn.execute(
            "SELECT estado, atendido_por, atendido_en, creado_en FROM tickets"
            " WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()

        if existente is None:
            self._conn.execute(
                "INSERT INTO tickets(ticket_id, llamada_id, creado_en, nivel, estado,"
                " motivo, reglas_json, citas_json, version_reglas, hoja_legible)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    ticket_id, llamada_id, creado, decision.nivel.value, "abierto",
                    decision.motivo,
                    json.dumps(reglas, ensure_ascii=False),
                    json.dumps(citas, ensure_ascii=False),
                    decision.version_reglas, hoja,
                ),
            )
        else:
            creado = existente["creado_en"]
            self._conn.execute(
                "UPDATE tickets SET nivel=?, motivo=?, reglas_json=?, citas_json=?,"
                " version_reglas=?, hoja_legible=? WHERE ticket_id=?",
                (
                    decision.nivel.value, decision.motivo,
                    json.dumps(reglas, ensure_ascii=False),
                    json.dumps(citas, ensure_ascii=False),
                    decision.version_reglas, hoja, ticket_id,
                ),
            )

        self._encolar_entregas(ticket_id)

        return {
            "ticket_id": ticket_id,
            "nivel": decision.nivel.value,
            "motivo": decision.motivo,
            "creado_en": creado,
            "hoja_legible": hoja,
        }

    # ------------------------------------------------------------------
    # Serie de mediciones por paciente
    # ------------------------------------------------------------------

    def _guardar_mediciones(
        self, llamada_id: str, policy: ContextoCierre | DialogPolicy, ahora: str
    ) -> None:
        """Materializa el cuadro de esta llamada como serie consultable."""

        p = policy.paciente
        for dominio in DOMINIOS_RESUMEN:
            obs = policy.estado.observacion(dominio)
            valor = None if obs.falta else str(obs.valor)
            self._conn.execute(
                "INSERT OR REPLACE INTO mediciones(llamada_id, paciente_id, dia_postop,"
                " dominio, valor, medido_en) VALUES(?,?,?,?,?,?)",
                (llamada_id, p.paciente_id, p.dia_postop, dominio, valor, ahora),
            )

    def historial(
        self, paciente_id: str, antes_de_dia: int | None = None, excluir: str = ""
    ) -> list[dict]:
        """Las mediciones anteriores de este paciente, en orden de dia postoperatorio.

        `excluir` deja fuera la llamada en curso, que es lo que se quiere al pedir
        "lo que sabiamos antes de hoy".
        """

        sql = (
            "SELECT llamada_id, dia_postop, dominio, valor, medido_en FROM mediciones"
            " WHERE paciente_id = ? AND valor IS NOT NULL"
        )
        params: list[object] = [paciente_id]
        if antes_de_dia is not None:
            sql += " AND dia_postop < ?"
            params.append(antes_de_dia)
        if excluir:
            sql += " AND llamada_id <> ?"
            params.append(excluir)
        sql += " ORDER BY dia_postop ASC, dominio ASC"
        return [dict(f) for f in self._conn.execute(sql, params).fetchall()]

    def serie_por_dominio(
        self, paciente_id: str, antes_de_dia: int | None = None, excluir: str = ""
    ) -> dict[str, list[tuple[int, str]]]:
        """El historial agrupado: dominio -> [(dia, valor), ...]."""

        agrupado: dict[str, list[tuple[int, str]]] = {}
        for m in self.historial(paciente_id, antes_de_dia, excluir):
            agrupado.setdefault(m["dominio"], []).append((m["dia_postop"], m["valor"]))
        return agrupado

    # ------------------------------------------------------------------
    # Outbox de entrega
    #
    # El ticket y su encolado se escriben en la misma transaccion que el cierre,
    # asi que no existe el estado "hay alerta pero nadie la va a entregar". El
    # despachador (escalation/despacho.py) es el unico que consume esta cola.
    # ------------------------------------------------------------------

    def registrar_canales(self, nombres: tuple[str, ...]) -> None:
        """Canales por los que hay que entregar cada alerta nueva."""

        self.canales = tuple(nombres)

    def _encolar_entregas(self, ticket_id: str) -> None:
        ahora = datetime.now(timezone.utc).isoformat()
        for canal in getattr(self, "canales", ("archivo",)):
            self._conn.execute(
                "INSERT OR IGNORE INTO entregas(ticket_id, canal, estado, creado_en,"
                " proximo_intento_en) VALUES(?,?,?,?,?)",
                (ticket_id, canal, "pendiente", ahora, ahora),
            )

    def entregas_pendientes(self, limite: int = 20) -> list[dict]:
        ahora = datetime.now(timezone.utc).isoformat()
        filas = self._conn.execute(
            "SELECT e.*, t.nivel, t.llamada_id, t.motivo, t.hoja_legible"
            " FROM entregas e JOIN tickets t ON t.ticket_id = e.ticket_id"
            " WHERE e.estado = 'pendiente' AND e.proximo_intento_en <= ?"
            " ORDER BY t.nivel = 'rojo' DESC, e.creado_en ASC LIMIT ?",
            (ahora, limite),
        ).fetchall()
        return [dict(f) for f in filas]

    def marcar_entregada(self, ticket_id: str, canal: str) -> None:
        ahora = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE entregas SET estado='entregado', entregado_en=?, ultimo_error=NULL,"
                " intentos=intentos+1 WHERE ticket_id=? AND canal=?",
                (ahora, ticket_id, canal),
            )
            self._conn.commit()

    def marcar_fallida(self, ticket_id: str, canal: str, error: str) -> None:
        """Reprograma el intento. Solo se rinde tras `MAX_INTENTOS`, y lo deja dicho."""

        with self._lock:
            fila = self._conn.execute(
                "SELECT intentos FROM entregas WHERE ticket_id=? AND canal=?",
                (ticket_id, canal),
            ).fetchone()
            intentos = (fila["intentos"] if fila else 0) + 1
            espera = min(ESPERA_BASE_S * (2 ** (intentos - 1)), ESPERA_MAXIMA_S)
            proximo = datetime.now(timezone.utc) + timedelta(seconds=espera)
            estado = "agotado" if intentos >= MAX_INTENTOS else "pendiente"
            self._conn.execute(
                "UPDATE entregas SET estado=?, intentos=?, proximo_intento_en=?,"
                " ultimo_error=? WHERE ticket_id=? AND canal=?",
                (estado, intentos, proximo.isoformat(), error[:400], ticket_id, canal),
            )
            self._conn.commit()

    def entregas_de(self, ticket_id: str) -> list[dict]:
        filas = self._conn.execute(
            "SELECT canal, estado, intentos, creado_en, entregado_en, ultimo_error,"
            " proximo_intento_en FROM entregas WHERE ticket_id=? ORDER BY canal",
            (ticket_id,),
        ).fetchall()
        return [dict(f) for f in filas]

    # ------------------------------------------------------------------
    # Acuse y plazos
    # ------------------------------------------------------------------

    def atender_ticket(self, ticket_id: str, quien: str) -> dict | None:
        """Acusa recibo. Es lo que convierte una bandeja en un turno de trabajo."""

        ahora = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tickets SET estado='atendido', atendido_por=?, atendido_en=?"
                " WHERE ticket_id=? AND estado <> 'atendido'",
                (quien, ahora, ticket_id),
            )
            self._conn.commit()
            cambiado = cur.rowcount > 0

        atendido: dict | None = None
        if cambiado:
            fila = self._conn.execute(
                "SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)
            ).fetchone()
            atendido = self._ticket_dict(fila) if fila else None
        return atendido

    def alertas_vencidas(
        self,
        sla_rojo_min: int = SLA_ROJO_MINUTOS,
        sla_amarillo_h: int = SLA_AMARILLO_HORAS,
    ) -> list[dict]:
        """Tickets sin acuse fuera de plazo.

        Sin esto, la bandeja crece y nadie se entera: hoy hay 83 tickets abiertos y
        cero atendidos, y el sistema no lo dice en ninguna parte.
        """

        ahora = datetime.now(timezone.utc)
        limite_rojo = (ahora - timedelta(minutes=sla_rojo_min)).isoformat()
        limite_amarillo = (ahora - timedelta(hours=sla_amarillo_h)).isoformat()
        filas = self._conn.execute(
            "SELECT * FROM tickets WHERE estado <> 'atendido' AND ("
            "  (nivel = 'rojo' AND creado_en <= ?)"
            "  OR (nivel = 'amarillo' AND creado_en <= ?))"
            " ORDER BY nivel = 'rojo' DESC, creado_en ASC",
            (limite_rojo, limite_amarillo),
        ).fetchall()
        return [self._ticket_dict(f) for f in filas]

    def tickets_sin_acuse_de(self, paciente_id: str, nivel: str = "rojo") -> list[dict]:
        """Alertas previas de este paciente que nadie atendio todavia.

        Se consulta al iniciar una llamada nueva: un rojo del dia 7 sin atender
        cuando llega la llamada del dia 14 es exactamente lo que no puede pasar en
        silencio.
        """

        filas = self._conn.execute(
            "SELECT t.* FROM tickets t JOIN llamadas l ON l.llamada_id = t.llamada_id"
            " WHERE l.paciente_id = ? AND t.nivel = ? AND t.estado <> 'atendido'"
            " ORDER BY t.creado_en DESC",
            (paciente_id, nivel),
        ).fetchall()
        return [self._ticket_dict(f) for f in filas]

    # ------------------------------------------------------------------
    # Resumen con forma FHIR
    # ------------------------------------------------------------------

    def construir_resumen(
        self,
        llamada_id: str,
        policy: ContextoCierre | DialogPolicy,
        decision: TriageDecision,
        citas: list[dict],
    ) -> dict:
        p = policy.paciente
        e = policy.estado
        ahora = datetime.now(timezone.utc).isoformat()
        serie = self.serie_por_dominio(p.paciente_id, p.dia_postop, excluir=llamada_id)
        alertas_previas = self.tickets_sin_acuse_de(p.paciente_id)

        observaciones = []
        for dominio in DOMINIOS_RESUMEN:
            obs = e.observacion(dominio)
            campo = {
                "dolor": "dolor_nrs", "fiebre": "fiebre_c", "movilidad": "movilidad",
                "herida": "herida", "apetito": "apetito", "sueno": "sueno",
            }[dominio]
            codigo, display = CODIGOS_LOINC[campo]
            registro = {
                "resourceType": "Observation",
                "status": "final" if obs.conocido else "registered",
                "code": {"coding": [{"system": "http://loinc.org", "code": codigo,
                                     "display": display}], "text": dominio},
                "subject": {"reference": f"Patient/{p.paciente_id}"},
                "effectiveDateTime": ahora,
                "dataAbsentReason": None if obs.conocido else {"text": "no reportado por el paciente"},
            }
            if obs.conocido:
                if dominio == "dolor":
                    registro["valueQuantity"] = {"value": obs.valor, "unit": "{score}"}
                elif dominio == "fiebre":
                    registro["valueQuantity"] = {"value": obs.valor, "unit": "Cel",
                                                 "system": "http://unitsofmeasure.org"}
                else:
                    registro["valueString"] = str(obs.valor)
            if obs.procedencia:
                registro["note"] = [{
                    "text": obs.procedencia.cita_paciente,
                    "authorString": "paciente",
                    "time": ahora,
                }]
                registro["_derivedFrom"] = {
                    "turno_idx": obs.procedencia.turno_idx,
                    "inferido": obs.procedencia.inferido,
                }
            observaciones.append(registro)

        resumen = {
            "resourceType": "Bundle",
            "type": "document",
            "id": llamada_id,
            "timestamp": ahora,
            "meta": {
                "source": "centinela",
                "versionReglas": decision.version_reglas,
                "advertencia": (
                    "Estructura con forma FHIR para facilitar integracion. "
                    "No es un bundle FHIR certificado."
                ),
            },
            "entry": [
                {
                    "resource": {
                        "resourceType": "Patient",
                        "id": p.paciente_id,
                        "name": [{"text": p.nombre}],
                        "address": [{"city": p.ciudad}] if p.ciudad else [],
                        "extension": [
                            {"url": "eps", "valueString": p.eps or ""},
                            {"url": "comorbilidades", "valueString": ", ".join(p.comorbilidades)},
                        ],
                    }
                },
                {
                    "resource": {
                        "resourceType": "Encounter",
                        "id": llamada_id,
                        "status": "finished",
                        "class": {"code": "VR", "display": "virtual"},
                        "type": [{"text": f"Seguimiento postoperatorio dia {p.dia_postop}"}],
                        "subject": {"reference": f"Patient/{p.paciente_id}"},
                        "reasonCode": [{"text": p.procedimiento}],
                        "period": {
                            "start": policy.iniciada_en.isoformat(),
                            "end": ahora,
                        },
                    }
                },
                *[{"resource": o} for o in observaciones],
                {
                    "resource": {
                        "resourceType": "RiskAssessment",
                        "status": "final",
                        "subject": {"reference": f"Patient/{p.paciente_id}"},
                        "prediction": [{
                            "qualitativeRisk": {"text": decision.nivel.value},
                            "rationale": decision.motivo,
                        }],
                        "basis": [
                            {
                                "codigo_regla": r.codigo,
                                "descripcion": r.descripcion,
                                "dominio": r.dominio,
                                "valor_observado": r.valor_observado,
                                "umbral": r.umbral,
                                "cita": r.cita.model_dump() if r.cita else None,
                            }
                            for r in decision.reglas_rojas + decision.banderas_amarillas
                        ],
                        "note": [{"text": f"Motor determinista {decision.version_reglas}"}],
                    }
                },
            ],
            "_centinela": {
                "nivel": decision.nivel.value,
                "escala": decision.escala,
                "dominios_no_respondidos": policy.estado.dominios_faltantes(),
                "sintomas_fuera_de_protocolo": policy.estado.sintomas_libres,
                "preguntas_sin_responder": policy.preguntas_sin_responder,
                "incidentes_de_seguridad": policy.incidentes,
                "citas_usadas": citas,
                # Lo que se le leyo de vuelta al paciente y como respondio. Un dato que
                # desmintio no se borra -- la alerta ya salio y decide una persona -- se
                # anota aqui para que quien reciba el caso lo vea.
                "confirmaciones": list(getattr(policy, "confirmaciones", []) or []),
                # Los valores que el paciente cambio a mitad de llamada, con las dos
                # versiones. Una correccion no retira una alerta ya creada: si pudiera,
                # bastaria decir un numero mas bajo para bajar la criticidad.
                "correcciones": [c.model_dump(mode="json") for c in e.correcciones],
                # Un rojo que el paciente no llego a oir no es el mismo rojo. La
                # instruccion se pudo cortar porque interrumpio al agente, y quien
                # recibe el caso tiene que saber si hay alguien yendo a urgencias o
                # alguien que no se entero.
                "urgencia_oida": getattr(policy, "urgencia_oida", None),
                "proximos_pasos": self._proximos_pasos(
                    decision, getattr(policy, "urgencia_oida", None)
                ),
                "consultas_rag": policy.consultas_rag,
                "n_turnos": len(policy.turnos),
                # La serie de las llamadas anteriores del mismo paciente. Va al
                # resumen y a la hoja de traspaso, no a una regla: sobre las 40
                # trayectorias oficiales una regla de delta no anticipa nada
                # (`eval/tendencia.py`), pero un dolor que va 4 -> 4 -> 9 es lo
                # primero que el humano que recibe la alerta necesita ver.
                "historial_previo": {
                    dominio: [{"dia": d, "valor": v} for d, v in valores]
                    for dominio, valores in serie.items()
                },
                "alertas_previas_sin_acuse": [
                    {
                        "ticket_id": t["ticket_id"],
                        "nivel": t["nivel"],
                        "creado_en": t["creado_en"],
                        "motivo": t["motivo"],
                    }
                    for t in alertas_previas
                    if t["llamada_id"] != llamada_id
                ],
            },
        }

        if decision.escala:
            resumen["entry"].append({
                "resource": {
                    "resourceType": "CommunicationRequest",
                    "status": "active",
                    "priority": PRIORIDAD_FHIR[decision.nivel.value],
                    "subject": {"reference": f"Patient/{p.paciente_id}"},
                    "reasonCode": [{"text": decision.motivo}],
                    "payload": [{"contentString": self._proximos_pasos(
                        decision, getattr(policy, "urgencia_oida", None))}],
                    "occurrenceDateTime": ahora,
                }
            })

        return resumen

    @staticmethod
    def _proximos_pasos(decision: TriageDecision, urgencia_oida: bool | None = None) -> str:
        if decision.nivel is Nivel.ROJO:
            # "Se indico al paciente acudir a urgencias" es una afirmacion, y solo es
            # cierta si el paciente la oyo. Con barge-in puede haber cortado al agente
            # justo ahi, y entonces lo que hay que hacer cambia: no es verificar que
            # llego, es decirselo.
            if urgencia_oida is False:
                pasos = (
                    "Contacto clinico inmediato. ATENCION: la indicacion de acudir a "
                    "urgencias NO se alcanzo a decir completa -- el paciente interrumpio "
                    "al agente. Hay que darsela por telefono antes de nada."
                )
            else:
                pasos = (
                    "Contacto clinico inmediato. Se indico al paciente acudir a urgencias "
                    "o llamar al 123. Requiere verificacion de que el paciente llego a "
                    "atencion."
                )
        elif decision.nivel is Nivel.AMARILLO:
            pasos = (
                "Contacto de enfermeria dentro de las proximas 24 horas. "
                "Revisar los hallazgos de vigilancia y decidir si amerita valoracion "
                "presencial."
            )
        else:
            pasos = (
                "Sin accion inmediata. Continuar con el calendario de seguimiento "
                "programado."
            )
        return pasos

    # ------------------------------------------------------------------

    def hoja_legible(
        self,
        policy: ContextoCierre | DialogPolicy,
        decision: TriageDecision,
        citas: list[dict],
        llamada_id: str = "",
    ) -> str:
        p = policy.paciente
        e = policy.estado
        serie = self.serie_por_dominio(p.paciente_id, p.dia_postop, excluir=llamada_id)
        L: list[str] = []

        etiqueta = {"rojo": "ALERTA ROJA", "amarillo": "SEGUIMIENTO", "verde": "SIN HALLAZGOS"}
        L.append(f"=== {etiqueta[decision.nivel.value]} ===")
        L.append("")
        L.append(f"Paciente      : {p.nombre}  (id {p.paciente_id})")
        L.append(f"Procedimiento : {p.procedimiento}  -  dia {p.dia_postop} postoperatorio")
        if p.edad is not None:
            L.append(f"Edad / genero : {p.edad} / {p.genero or '?'}")
        if p.comorbilidades:
            L.append(f"Comorbilidades: {', '.join(p.comorbilidades)}")
        if p.eps:
            L.append(f"EPS / ciudad  : {p.eps}  -  {p.ciudad or '?'}")
        L.append("")
        L.append("CUADRO REPORTADO POR EL PACIENTE")
        for dominio in DOMINIOS_RESUMEN:
            obs = e.observacion(dominio)
            previo = serie.get(dominio, [])
            # La serie de dias anteriores al lado del valor de hoy. Un 9 no dice lo
            # mismo si venia de un 4 que si venia de un 8.
            historia = ""
            if previo:
                historia = "   [" + " · ".join(f"d{d}: {v}" for d, v in previo) + "]"
            if obs.conocido:
                cita = obs.procedencia.cita_paciente if obs.procedencia else ""
                L.append(f"  {dominio:10s}: {obs.valor}{historia}")
                if cita:
                    L.append(f"              (dijo: \"{cita[:110]}\")")
            else:
                L.append(f"  {dominio:10s}: NO REPORTADO{historia}")
        if e.fiebre_subjetiva and e.fiebre_c.falta:
            L.append("  nota      : refiere sensacion febril sin medicion objetiva")
        if e.sintomas_libres:
            L.append(f"  otros     : {'; '.join(e.sintomas_libres)}")
        L.append("")
        L.append(f"DECISION: {decision.nivel.value.upper()}   (motor {decision.version_reglas})")
        L.append(f"  {decision.motivo}")
        if decision.reglas_rojas:
            L.append("")
            L.append("  Criterios de alarma cumplidos:")
            for r in decision.reglas_rojas:
                L.append(f"    - [{r.codigo}] {r.descripcion}")
                L.append(f"        observado: {r.valor_observado}   umbral: {r.umbral}")
                if r.cita and r.cita.documento:
                    L.append(f"        respaldo: {r.cita.documento}, pag. {r.cita.pagina}")
        if decision.banderas_amarillas:
            L.append("")
            L.append("  Banderas de vigilancia:")
            for r in decision.banderas_amarillas:
                L.append(f"    - [{r.codigo}] {r.descripcion} (observado: {r.valor_observado})")
        # Alertas anteriores de este mismo paciente que nadie atendio. Va antes de
        # las preguntas pendientes porque es lo mas urgente que puede aparecer en
        # esta hoja: significa que el sistema ya aviso y no paso nada.
        previas = [
            t for t in self.tickets_sin_acuse_de(p.paciente_id)
            if t["llamada_id"] != llamada_id
        ]
        if previas:
            L.append("")
            L.append("  ALERTAS ANTERIORES DE ESTE PACIENTE SIN ACUSE:")
            for t in previas:
                L.append(f"    - [{t['ticket_id']}] {t['nivel'].upper()} del {t['creado_en'][:16]}")
                L.append(f"        {t['motivo'][:110]}")

        if e.correcciones:
            L.append("")
            L.append("  DATOS QUE EL PACIENTE CORRIGIO DURANTE LA LLAMADA:")
            for c in e.correcciones:
                L.append(
                    f"    - {c.dominio}: {c.valor_anterior} -> {c.valor_nuevo} "
                    f"(turno {c.turno_idx})"
                )
                if c.cita_paciente:
                    L.append(f"        dijo: «{c.cita_paciente[:110]}»")

        # Lo desmentido va primero y con mayusculas, porque cambia como se lee todo lo
        # de arriba: un hallazgo que el paciente nego al confirmarlo sigue en la alerta
        # -- no se retira por una respuesta ambigua -- pero quien llame tiene que
        # saberlo antes de repetirle al paciente un dato que el ya discutio.
        desmentidas = [
            c for c in (getattr(policy, "confirmaciones", []) or [])
            if c.get("desenlace") in ("desmentido", "sin_respuesta")
        ]
        if desmentidas:
            L.append("")
            L.append("  LO QUE EL PACIENTE NO CONFIRMO AL RELEERSELO:")
            for c in desmentidas:
                que = "lo desmintio" if c["desenlace"] == "desmentido" else "no contesto"
                L.append(f"    - «{c.get('leido')}» -- {que} (turno {c.get('turno_idx')})")
            L.append("        El hallazgo se mantiene en la alerta: lo verifica una persona.")

        if policy.preguntas_sin_responder:
            L.append("")
            L.append("  PREGUNTAS DEL PACIENTE QUE QUEDARON SIN RESPONDER:")
            for q in policy.preguntas_sin_responder:
                L.append(f"    - {q}")
        if policy.incidentes:
            L.append("")
            L.append("  Incidentes de seguridad durante la llamada:")
            for i in policy.incidentes:
                L.append(f"    - {i}")
        if citas:
            L.append("")
            L.append("  Referencias clinicas citadas al paciente:")
            vistos = set()
            for c in citas:
                clave = (c.get("documento"), c.get("pagina"))
                if clave not in vistos:
                    vistos.add(clave)
                    L.append(f"    - {c.get('documento')} , pag. {c.get('pagina')}")
        L.append("")
        L.append("PROXIMOS PASOS")
        L.append(f"  {self._proximos_pasos(decision, getattr(policy, 'urgencia_oida', None))}")
        return "\n".join(L)

    @staticmethod
    def _transcribir(policy: ContextoCierre | DialogPolicy) -> str:
        lineas = [
            f"[{t.turno_idx:02d}] {t.hablante:8s} | {t.texto}"
            for t in policy.turnos
        ]
        return "\n".join(lineas)

    # ------------------------------------------------------------------
    # Consultas para la consola
    # ------------------------------------------------------------------

    def tickets(self, estado: str | None = None, limite: int = 50) -> list[dict]:
        if estado:
            filas = self._conn.execute(
                "SELECT * FROM tickets WHERE estado = ? ORDER BY creado_en DESC LIMIT ?",
                (estado, limite),
            ).fetchall()
        else:
            filas = self._conn.execute(
                "SELECT * FROM tickets ORDER BY creado_en DESC LIMIT ?", (limite,)
            ).fetchall()

        # El estado de entrega viaja con el ticket: en la consola, "hay alerta" y
        # "la alerta salio" son dos hechos distintos y hay que poder distinguirlos.
        salida = []
        for f in filas:
            d = self._ticket_dict(f)
            d["entregas"] = self.entregas_de(d["ticket_id"])
            salida.append(d)
        return salida

    def llamadas(self, limite: int = 50) -> list[dict]:
        filas = self._conn.execute(
            "SELECT llamada_id, paciente_id, nombre, procedimiento, dia_postop,"
            " iniciada_en, terminada_en, nivel_final, n_turnos"
            " FROM llamadas ORDER BY iniciada_en DESC LIMIT ?",
            (limite,),
        ).fetchall()
        return [dict(f) for f in filas]

    def llamada(self, llamada_id: str) -> dict | None:
        fila = self._conn.execute(
            "SELECT * FROM llamadas WHERE llamada_id = ?", (llamada_id,)
        ).fetchone()
        if fila is None:
            salida = None
        else:
            salida = dict(fila)
            if salida.get("resumen_json"):
                salida["resumen"] = json.loads(salida["resumen_json"])
        return salida

    def turnos_persistidos(self, llamada_id: str) -> list[dict]:
        """Los turnos tal como quedaron EN DISCO.

        Se publica en la traza junto a los de memoria a proposito: los dos deben decir
        lo mismo, y cuando no lo dicen es que algo se corrigio en memoria y no bajo al
        registro. Paso exactamente eso con los turnos interrumpidos, y sin poder
        comparar los dos no habia forma de verlo desde fuera.
        """

        filas = self._conn.execute(
            "SELECT turno_idx, hablante, momento, texto, nivel FROM turnos"
            " WHERE llamada_id = ? ORDER BY turno_idx",
            (llamada_id,),
        ).fetchall()
        return [dict(f) for f in filas]

    @staticmethod
    def _ticket_dict(f: sqlite3.Row) -> dict:
        d = dict(f)
        d["reglas"] = json.loads(d.pop("reglas_json"))
        d["citas"] = json.loads(d.pop("citas_json"))
        return d

    def cerrar(self) -> None:
        self._conn.close()
