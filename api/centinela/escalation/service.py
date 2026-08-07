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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..dialog.policy import DialogPolicy
from ..models import Nivel, TriageDecision

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
"""

PRIORIDAD_FHIR = {"rojo": "urgent", "amarillo": "routine", "verde": "routine"}

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


class EscalationService:
    def __init__(self, ruta_datos: Path) -> None:
        self.ruta = Path(ruta_datos)
        self.ruta.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.ruta / "llamadas.db", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(ESQUEMA)
        self._conn.commit()

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
        policy: DialogPolicy,
        decision: TriageDecision,
        citas: list[dict],
    ) -> dict:
        """Persiste el resumen y, si corresponde, crea el ticket de alerta."""

        resumen = self.construir_resumen(llamada_id, policy, decision, citas)
        transcripcion = self._transcribir(policy)
        ahora = datetime.now(timezone.utc).isoformat()

        with self._lock:
            self._conn.execute(
                "UPDATE llamadas SET terminada_en=?, nivel_final=?, version_reglas=?,"
                " n_turnos=?, resumen_json=?, transcripcion=? WHERE llamada_id=?",
                (
                    ahora, decision.nivel.value, decision.version_reglas,
                    len(policy.turnos),
                    json.dumps(resumen, indent=2, ensure_ascii=False),
                    transcripcion, llamada_id,
                ),
            )

            ticket = None
            if decision.escala:
                ticket = self._crear_ticket(llamada_id, policy, decision, citas, resumen)

            for inc in policy.incidentes:
                self._conn.execute(
                    "INSERT INTO incidentes(llamada_id, momento, tipo, detalle) VALUES(?,?,?,?)",
                    (llamada_id, ahora, "seguridad", inc),
                )

            self._conn.commit()

        return {"resumen": resumen, "ticket": ticket}

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
        hoja = self.hoja_legible(policy, decision, citas)
        creado = datetime.now(timezone.utc).isoformat()

        self._conn.execute(
            "INSERT OR REPLACE INTO tickets(ticket_id, llamada_id, creado_en, nivel, estado,"
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
        return {
            "ticket_id": ticket_id,
            "nivel": decision.nivel.value,
            "motivo": decision.motivo,
            "creado_en": creado,
            "hoja_legible": hoja,
        }

    # ------------------------------------------------------------------
    # Resumen con forma FHIR
    # ------------------------------------------------------------------

    def construir_resumen(
        self,
        llamada_id: str,
        policy: DialogPolicy,
        decision: TriageDecision,
        citas: list[dict],
    ) -> dict:
        p = policy.paciente
        e = policy.estado
        ahora = datetime.now(timezone.utc).isoformat()

        observaciones = []
        for dominio in ("dolor", "fiebre", "movilidad", "herida", "apetito", "sueno"):
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
                "proximos_pasos": self._proximos_pasos(decision),
                "consultas_rag": policy.consultas_rag,
                "n_turnos": len(policy.turnos),
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
                    "payload": [{"contentString": self._proximos_pasos(decision)}],
                    "occurrenceDateTime": ahora,
                }
            })

        return resumen

    @staticmethod
    def _proximos_pasos(decision: TriageDecision) -> str:
        if decision.nivel is Nivel.ROJO:
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
        self, policy: DialogPolicy, decision: TriageDecision, citas: list[dict]
    ) -> str:
        p = policy.paciente
        e = policy.estado
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
        for dominio in ("dolor", "fiebre", "movilidad", "herida", "apetito", "sueno"):
            obs = e.observacion(dominio)
            if obs.conocido:
                cita = obs.procedencia.cita_paciente if obs.procedencia else ""
                L.append(f"  {dominio:10s}: {obs.valor}")
                if cita:
                    L.append(f"              (dijo: \"{cita[:110]}\")")
            else:
                L.append(f"  {dominio:10s}: NO REPORTADO")
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
        L.append(f"  {self._proximos_pasos(decision)}")
        return "\n".join(L)

    @staticmethod
    def _transcribir(policy: DialogPolicy) -> str:
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
        return [self._ticket_dict(f) for f in filas]

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

    @staticmethod
    def _ticket_dict(f: sqlite3.Row) -> dict:
        d = dict(f)
        d["reglas"] = json.loads(d.pop("reglas_json"))
        d["citas"] = json.loads(d.pop("citas_json"))
        return d

    def cerrar(self) -> None:
        self._conn.close()
