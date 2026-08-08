"""«Le repito para estar seguro de que le entendi bien.»

Lo que se prueba aqui es la comunicacion de circuito cerrado, y lo que hay que probar
no es que el agente sea educado: es que confirmar **no debilite** el escalamiento.

Hay dos formas de equivocarse con esta funcion, y las dos son peores que no tenerla:

  1. **Que la alerta espere la confirmacion.** El ticket nace en el turno en que aparece
     la bandera. Si confirmar lo retrasara, un paciente que cuelga durante la
     confirmacion se llevaria su alerta con el -- que es exactamente el agujero que se
     cerro en su dia y que no se puede reabrir por la puerta de al lado.
  2. **Que un "no" apague el ticket.** El paciente que minimiza es uno de los perfiles
     del reto. Si "no, ya se me quito" retirara la bandera, la confirmacion seria una
     puerta trasera para bajar la criticidad.

Y una tercera, mas sutil: **que no conseguir confirmacion bloquee la instruccion**.
Alguien con 38.5 que no contesta si ni no tiene que oir que vaya a urgencias igual.

Los cuatro desenlaces: confirma, desmiente, no contesta, y contesta corrigiendo.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from centinela.clinical.extractor import ResultadoExtraccion  # noqa: E402
from centinela.clinical.normalizer import normalizar_turno  # noqa: E402
from centinela.clinical.triage_engine import TriageEngine  # noqa: E402
from centinela.dialog import script as S  # noqa: E402
from centinela.dialog.confirmacion import (  # noqa: E402
    AFIRMA,
    MOTIVO_ESCALA,
    MOTIVO_INFERIDO,
    NI_UNA_COSA_NI_LA_OTRA,
    NIEGA,
    frase_de,
    interpretar,
    que_confirmar,
)
from centinela.dialog.policy import (  # noqa: E402
    PAPEL_URGENTE,
    DialogPolicy,
    EstadoLlamada,
    Paciente,
)
from centinela.models import ClinicalState, Nivel, Procedencia  # noqa: E402


class ExtractorGuionado:
    """Extrae segun un guion fijo. Sin modelo y sin sorpresas.

    Cada entrada dice: si el turno contiene X, pon este valor en este dominio, marcado
    como explicito o como inferido. Es lo que permite probar el disparador de valor
    inferido sin depender de lo que decida un modelo de lenguaje.
    """

    def __init__(self, guion) -> None:
        self.guion = guion

    async def extraer(self, texto_paciente, estado, turno_idx, pregunta_agente="",
                      dominio_objetivo=""):
        norm = normalizar_turno(texto_paciente, dominio_objetivo)
        bajo = texto_paciente.lower()
        for gatillo, dominio, valor, inferido in self.guion:
            if gatillo in bajo:
                obs = estado.observacion(dominio)
                obs.valor = valor
                obs.conocido = True
                obs.procedencia = Procedencia(
                    turno_idx=turno_idx, cita_paciente=texto_paciente, inferido=inferido
                )
        return ResultadoExtraccion(estado=estado, normalizado=norm, respondio=True)


# Un dolor de 7 ya es bandera roja (`DOLOR_ROJO_NRS`), asi que para probar el
# disparador del valor inferido hace falta una cifra que NO escale: si no, los dos
# disparadores se solapan y el test no distingue cual actuo.
FIEBRE_ALTA = [("fiebre", "fiebre", 38.5, False)]
DOLOR_INFERIDO = [("mas o menos", "dolor", 4.0, True)]
DOLOR_EXPLICITO = [("cuatro", "dolor", 4.0, False)]


def nueva_policy(guion) -> DialogPolicy:
    policy = DialogPolicy(
        paciente=Paciente(
            paciente_id="pac_test", nombre="Paciente Test",
            procedimiento="Colecistectomía", dia_postop=7,
        ),
        extractor=ExtractorGuionado(guion),
        motor=TriageEngine(),
    )
    policy.abrir()
    return policy


# ==========================================================================
# La frase que se lee de vuelta
# ==========================================================================

def test_la_frase_suena_a_persona_no_a_protocolo() -> None:
    """El enunciado del umbral no sirve: "fiebre igual o mayor a 38.0" no se dice."""

    assert frase_de("fiebre", 38.5) == "fiebre de 38.5 grados"
    assert frase_de("dolor", 7) == "un dolor de 7 sobre 10"
    assert frase_de("dolor", 7.0) == "un dolor de 7 sobre 10", "sin el .0 de mas"
    assert frase_de("herida", "secrecion_purulenta") == "liquido amarillo o pus saliendo de la herida"
    assert frase_de("movilidad", "incapacitante_nueva") == "que no puede caminar como antes"
    assert frase_de("fiebre", None) == ""
    assert frase_de("inventado", "cosa") == ""


def test_varios_hallazgos_se_enlazan_como_al_hablar() -> None:
    estado = ClinicalState()
    estado.fiebre_c.valor = 38.6
    estado.fiebre_c.conocido = True
    estado.herida.valor = "secrecion_purulenta"
    estado.herida.conocido = True
    decision = TriageEngine().evaluar(estado, cerrar=False)

    conf = que_confirmar(estado, decision)

    assert conf is not None
    assert conf.motivo == MOTIVO_ESCALA
    assert " y " in conf.frase
    assert "fiebre de 38.6 grados" in conf.frase


def test_un_verde_no_tiene_nada_que_confirmar() -> None:
    estado = ClinicalState()
    estado.dolor_nrs.valor = 2.0
    estado.dolor_nrs.conocido = True
    estado.dolor_nrs.procedencia = Procedencia(turno_idx=1, cita_paciente="un dos")
    decision = TriageEngine().evaluar(estado, cerrar=False)

    assert que_confirmar(estado, decision, turno_idx=1) is None


def test_solo_se_confirma_lo_captado_en_este_turno() -> None:
    """Sin el filtro, un valor inferido en el turno dos se reconfirma en el cinco."""

    estado = ClinicalState()
    estado.dolor_nrs.valor = 4.0
    estado.dolor_nrs.conocido = True
    estado.dolor_nrs.procedencia = Procedencia(
        turno_idx=2, cita_paciente="pues mas o menos", inferido=True
    )
    decision = TriageEngine().evaluar(estado, cerrar=False)

    assert que_confirmar(estado, decision, turno_idx=2) is not None
    assert que_confirmar(estado, decision, turno_idx=5) is None


# ==========================================================================
# Interpretar el si o el no
# ==========================================================================

@pytest.mark.parametrize("texto", [
    "Si", "si senor", "Claro", "exacto", "asi es", "correcto",
    "Si, eso mismo", "de acuerdo", "por supuesto", "aja",
])
def test_afirmaciones(texto: str) -> None:
    assert interpretar(texto) == AFIRMA


@pytest.mark.parametrize("texto", [
    "No", "no, para nada", "incorrecto", "me equivoque", "eso no",
    "no es asi", "nada que ver", "que no", "esta mal",
])
def test_negaciones(texto: str) -> None:
    assert interpretar(texto) == NIEGA


def test_la_negacion_gana_el_empate() -> None:
    """"no, si, o sea, no exactamente" es un desmentido con ruido, no un si."""

    assert interpretar("no, si, o sea, no exactamente") == NIEGA


@pytest.mark.parametrize("texto", [
    "no se", "no lo se", "no me acuerdo", "pues no sabria decirle",
])
def test_un_no_lo_se_no_es_un_desmentido(texto: str) -> None:
    assert interpretar(texto) == NI_UNA_COSA_NI_LA_OTRA


def test_una_cifra_nueva_del_mismo_dominio_desmiente() -> None:
    """El paciente corrige en vez de discutir: "era treinta y seis y medio"."""

    assert interpretar("era treinta y seis y medio", "fiebre") == NIEGA
    assert interpretar("como un tres", "dolor") == NIEGA
    # Sin dominio no hay contra que medir, y no se adivina.
    assert interpretar("como un tres") == NI_UNA_COSA_NI_LA_OTRA


def test_un_silencio_no_afirma_nada() -> None:
    assert interpretar("") == NI_UNA_COSA_NI_LA_OTRA
    assert interpretar("   ") == NI_UNA_COSA_NI_LA_OTRA


# ==========================================================================
# La bandera roja se confirma antes de mandar a urgencias
# ==========================================================================

@pytest.mark.asyncio
async def test_el_rojo_lee_de_vuelta_antes_de_escalar() -> None:
    policy = nueva_policy(FIEBRE_ALTA)
    await policy.procesar("Si, soy yo")

    bandera = await policy.procesar("Si, he tenido fiebre de 38.5")

    assert bandera.decision.nivel is Nivel.ROJO
    assert policy.fase is EstadoLlamada.CONFIRMANDO
    claves = [f.clave for f in bandera.fragmentos]
    assert S.CONFIRMAR_ENTENDIDO.clave in claves
    assert S.CONFIRMAR_PREGUNTA.clave in claves
    assert "fiebre de 38.5 grados" in bandera.texto_completo
    # Todavia NO se ha mandado a nadie a urgencias.
    assert S.CIERRE_ROJO.clave not in claves
    assert bandera.llamada_terminada is False


@pytest.mark.asyncio
async def test_la_alerta_no_espera_la_confirmacion() -> None:
    """Lo que confirmar cambia es lo que el paciente OYE, no lo que el equipo RECIBE."""

    policy = nueva_policy(FIEBRE_ALTA)
    await policy.procesar("Si, soy yo")
    bandera = await policy.procesar("Si, he tenido fiebre de 38.5")

    # `escala_ahora` es lo que dispara la creacion del ticket en `_empaquetar_turno`.
    assert bandera.escala_ahora is True
    assert bandera.decision.escala is True


@pytest.mark.asyncio
async def test_confirmado_se_manda_a_urgencias_sin_repetir_el_preambulo() -> None:
    policy = nueva_policy(FIEBRE_ALTA)
    await policy.procesar("Si, soy yo")
    await policy.procesar("Si, he tenido fiebre de 38.5")

    tras = await policy.procesar("Si, correcto")

    claves = [f.clave for f in tras.fragmentos]
    assert S.CONFIRMACION_ACEPTADA.clave in claves
    assert S.CIERRE_ROJO.clave in claves
    assert S.INTERRUPCION_ROJA.clave not in claves, "ya se dijo en el turno anterior"
    assert tras.llamada_terminada is True
    assert any(f.papel == PAPEL_URGENTE for f in tras.fragmentos)
    assert policy.confirmaciones[-1]["desenlace"] == "confirmado"


@pytest.mark.asyncio
async def test_desmentido_la_alerta_se_queda_y_se_vuelve_a_preguntar() -> None:
    policy = nueva_policy(FIEBRE_ALTA)
    await policy.procesar("Si, soy yo")
    await policy.procesar("Si, he tenido fiebre de 38.5")

    tras = await policy.procesar("No, no es asi")

    # El dato NO se borra: la alerta ya existe y decide una persona.
    assert policy.estado.fiebre_c.valor == 38.5
    assert policy.confirmaciones[-1]["desenlace"] == "desmentido"
    assert policy.confirmaciones[-1]["dominios"] == ["fiebre"]
    # Y se vuelve a preguntar el dominio, con la formulacion inicial y sin gastar intento.
    claves = [f.clave for f in tras.fragmentos]
    assert S.CONFIRMACION_DESMENTIDA.clave in claves
    assert S.PREGUNTA_POR_DOMINIO["fiebre"].inicial.clave in claves
    assert policy.intentos.get("fiebre", 0) == 0
    assert tras.llamada_terminada is False
    assert policy.fase is EstadoLlamada.PREGUNTANDO


@pytest.mark.asyncio
async def test_no_conseguir_confirmacion_no_bloquea_la_instruccion() -> None:
    """Se pregunta una vez mas, y despues se actua. Nunca se queda esperando."""

    policy = nueva_policy(FIEBRE_ALTA)
    await policy.procesar("Si, soy yo")
    await policy.procesar("Si, he tenido fiebre de 38.5")

    reintento = await policy.procesar("como?")
    assert S.CONFIRMAR_PREGUNTA.clave in [f.clave for f in reintento.fragmentos]
    assert policy.fase is EstadoLlamada.CONFIRMANDO

    tras = await policy.procesar("mmm")
    assert S.CIERRE_ROJO.clave in [f.clave for f in tras.fragmentos]
    assert tras.llamada_terminada is True
    assert policy.confirmaciones[-1]["desenlace"] == "sin_respuesta"


@pytest.mark.asyncio
async def test_no_se_pregunta_por_la_confirmacion_tres_veces() -> None:
    policy = nueva_policy(FIEBRE_ALTA)
    await policy.procesar("Si, soy yo")
    await policy.procesar("Si, he tenido fiebre de 38.5")
    await policy.procesar("como?")
    await policy.procesar("no le entiendo")

    veces = sum(
        1 for t in policy.turnos
        if t.hablante == "agente" and S.CONFIRMAR_PREGUNTA.texto in t.texto
    )
    assert veces == 2, "una vez y un reintento; una tercera suena a maquina atascada"


# ==========================================================================
# El numero que nadie dijo
# ==========================================================================

@pytest.mark.asyncio
async def test_un_dolor_inferido_se_lee_de_vuelta() -> None:
    policy = nueva_policy(DOLOR_INFERIDO)
    await policy.procesar("Si, soy yo")

    turno = await policy.procesar("pues mas o menos, ahi va")

    assert policy.fase is EstadoLlamada.CONFIRMANDO
    assert "un dolor de 4 sobre 10" in turno.texto_completo
    assert policy.confirmacion_pendiente.motivo == MOTIVO_INFERIDO
    # No se ha pasado al dominio siguiente todavia.
    assert policy.indice_dominio == 0


@pytest.mark.asyncio
async def test_un_dolor_explicito_no_se_lee_de_vuelta() -> None:
    """Confirmar cada cifra que el paciente dijo duplicaria los turnos por nada."""

    policy = nueva_policy(DOLOR_EXPLICITO)
    await policy.procesar("Si, soy yo")

    turno = await policy.procesar("como un cuatro")

    assert policy.fase is EstadoLlamada.PREGUNTANDO
    assert S.CONFIRMAR_ENTENDIDO.clave not in [f.clave for f in turno.fragmentos]
    assert policy.indice_dominio == 1, "paso al dominio siguiente"


@pytest.mark.asyncio
async def test_confirmado_el_valor_inferido_la_llamada_sigue() -> None:
    policy = nueva_policy(DOLOR_INFERIDO)
    await policy.procesar("Si, soy yo")
    await policy.procesar("pues mas o menos, ahi va")

    tras = await policy.procesar("si, mas o menos eso")

    assert policy.fase is EstadoLlamada.PREGUNTANDO
    assert policy.indice_dominio == 1
    assert S.PREGUNTA_POR_DOMINIO["fiebre"].inicial.clave in [
        f.clave for f in tras.fragmentos
    ]
    assert policy.estado.dolor_nrs.valor == 4.0


@pytest.mark.asyncio
async def test_desmentido_el_valor_inferido_se_vuelve_a_preguntar_el_dolor() -> None:
    policy = nueva_policy(DOLOR_INFERIDO)
    await policy.procesar("Si, soy yo")
    await policy.procesar("pues mas o menos, ahi va")

    tras = await policy.procesar("no, tanto no")

    assert S.PREGUNTA_POR_DOMINIO["dolor"].inicial.clave in [
        f.clave for f in tras.fragmentos
    ]
    assert policy.indice_dominio == 0
    assert policy.confirmaciones[-1]["desenlace"] == "desmentido"


@pytest.mark.asyncio
async def test_la_confirmacion_no_se_repite_en_cada_turno() -> None:
    """El valor sigue marcado como inferido; confirmarlo otra vez seria un bucle."""

    policy = nueva_policy(DOLOR_INFERIDO)
    await policy.procesar("Si, soy yo")
    await policy.procesar("pues mas o menos, ahi va")
    await policy.procesar("si, eso es")
    tras = await policy.procesar("no he tenido fiebre")

    assert S.CONFIRMAR_ENTENDIDO.clave not in [f.clave for f in tras.fragmentos]
