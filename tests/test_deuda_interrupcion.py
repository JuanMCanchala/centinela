"""Lo que el paciente no oyo no se dio por dicho.

Este es el archivo clinico del barge-in, y lo que prueba no es obvio.

`DialogPolicy` muta su estado **cuando construye los fragmentos**, no cuando el
paciente los oye: `_siguiente_pregunta` ya avanzo el dominio y
`_repetir_pregunta_actual` ya cargo un intento antes de que el audio salga por el
altavoz. Con barge-in eso deja de ser inocuo. Si el paciente corta al agente a media
pregunta, la politica cree haberla hecho.

Y no es un detalle de contabilidad: agotar `MAX_INTENTOS_POR_DOMINIO` marca el
dominio como desconocido, y un dominio desconocido al cerrar fuerza amarillo. Sin la
correccion que se prueba aqui, **tres interrupciones producirian una alerta que el
paciente no causo** -- generada por el transporte de audio, no por su estado de
salud. Es exactamente el tipo de falso positivo que hace que un puesto de enfermeria
deje de mirar la bandeja.

Las tres propiedades:

  1. Una pregunta no oida se vuelve a hacer, con la formulacion inicial y sin gastar
     un intento.
  2. Una respuesta del corpus no oida se debe y se paga en el turno siguiente. Un
     "aja" no oido no se debe: repetirlo suena a cinta.
  3. El registro clinico dice lo que se oyo, no lo que se planeo decir.
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
from centinela.dialog.policy import (  # noqa: E402
    MAX_INTENTOS_POR_DOMINIO,
    PAPEL_CONTENIDO,
    PAPEL_PREGUNTA,
    PAPEL_SOCIAL,
    DialogPolicy,
    Fragmento,
    Paciente,
)
from centinela.models import Nivel  # noqa: E402


class ExtractorMudo:
    """No extrae nada. El paciente habla y ningun dominio queda resuelto.

    Es el escenario que importa: si el paciente no respondio la pregunta, la
    politica vuelve a preguntar, y ahi se ve si el intento se cargo o no.
    """

    async def extraer(self, texto_paciente, estado, turno_idx, pregunta_agente="",
                      dominio_objetivo="", **_):
        return ResultadoExtraccion(
            estado=estado, normalizado=normalizar_turno(texto_paciente), respondio=False
        )


def nueva_policy(responder=None) -> DialogPolicy:
    policy = DialogPolicy(
        paciente=Paciente(
            paciente_id="pac_test", nombre="Paciente Test",
            procedimiento="Apendicectomía", dia_postop=7,
        ),
        extractor=ExtractorMudo(),
        motor=TriageEngine(),
        responder_clinico=responder,
    )
    policy.abrir()
    return policy


async def hasta_la_primera_pregunta(policy: DialogPolicy):
    """Deja la llamada justo despues de que el agente pregunte por el dolor."""

    return await policy.procesar("Si, soy yo")


# ==========================================================================
# El corte cae entre fragmentos
# ==========================================================================

def test_los_fragmentos_llevan_su_papel() -> None:
    """El papel es lo que decide que se repite. Si no esta, no hay nada que decidir."""

    policy = nueva_policy()
    accion = policy.ultima_accion

    assert accion is not None
    assert accion.fragmentos[0].papel == PAPEL_SOCIAL, "el saludo es social"
    assert accion.fragmentos[1].papel == PAPEL_CONTENIDO, "confirmar identidad es contenido"


@pytest.mark.asyncio
async def test_la_pregunta_del_guion_se_marca_como_pregunta() -> None:
    policy = nueva_policy()
    accion = await hasta_la_primera_pregunta(policy)

    preguntas = [f for f in accion.fragmentos if f.papel == PAPEL_PREGUNTA]
    assert len(preguntas) == 1
    assert preguntas[0].dominio == "dolor"


def test_marcar_interrumpido_sin_accion_previa_no_revienta() -> None:
    policy = DialogPolicy(
        paciente=Paciente("p", "N", "Apendicectomía", 7),
        extractor=ExtractorMudo(),
        motor=TriageEngine(),
    )
    assert policy.marcar_interrumpido(3)["fragmentos_dichos"] == 0


def test_pasarse_de_fragmentos_no_revienta() -> None:
    """El cliente reporta lo que reprodujo; un desajuste no puede tirar la llamada."""

    policy = nueva_policy()
    resultado = policy.marcar_interrumpido(99)

    assert resultado["fragmentos_en_deuda"] == 0
    assert resultado["fragmentos_dichos"] == 2


# ==========================================================================
# 1 · Una pregunta no oida es una pregunta no hecha
# ==========================================================================

@pytest.mark.asyncio
async def test_la_pregunta_cortada_se_vuelve_a_hacer_desde_cero() -> None:
    """Y con la formulacion inicial: "le repito" algo que nunca se oyo es mentira."""

    policy = nueva_policy()
    await hasta_la_primera_pregunta(policy)

    # Se oyo la transicion, no la pregunta.
    policy.marcar_interrumpido(1)
    assert "dolor" in policy.repetir_inicial

    siguiente = await policy.procesar("perdon, que decia?")
    claves = [f.clave for f in siguiente.fragmentos]

    assert S.PREGUNTA_POR_DOMINIO["dolor"].inicial.clave in claves
    assert S.PREGUNTA_POR_DOMINIO["dolor"].reintento.clave not in claves


@pytest.mark.asyncio
async def test_una_interrupcion_no_gasta_un_intento() -> None:
    policy = nueva_policy()
    await hasta_la_primera_pregunta(policy)
    policy.marcar_interrumpido(1)

    await policy.procesar("perdon?")

    assert policy.intentos.get("dolor", 0) == 0, (
        "la interrupcion cargo un intento que el paciente no gasto"
    )


@pytest.mark.asyncio
async def test_tres_interrupciones_no_inventan_un_amarillo() -> None:
    """La propiedad por la que existe todo este archivo.

    Sin la devolucion del intento, tres cortes agotarian el dominio, lo dejarian
    como desconocido, y un dominio desconocido al cerrar fuerza amarillo. La alerta
    la habria producido el transporte de audio, no el paciente.
    """

    policy = nueva_policy()
    await hasta_la_primera_pregunta(policy)

    for _ in range(3):
        policy.marcar_interrumpido(0)
        await policy.procesar("perdon, no le oi")

    assert policy.intentos.get("dolor", 0) <= MAX_INTENTOS_POR_DOMINIO - 1
    assert policy._dominio_actual() == "dolor", "no puede haber pasado de dominio"


@pytest.mark.asyncio
async def test_un_intento_de_verdad_si_se_cuenta() -> None:
    """El contrapeso: si el paciente OYO la pregunta y no la respondio, cuenta."""

    policy = nueva_policy()
    await hasta_la_primera_pregunta(policy)

    await policy.procesar("es que no se como explicarlo")

    assert policy.intentos.get("dolor", 0) == 1


@pytest.mark.asyncio
async def test_la_pregunta_no_oida_borra_el_contexto_del_extractor() -> None:
    """`ultima_pregunta` es lo que el extractor cree que el paciente esta respondiendo.

    Si no la oyo, no esta respondiendo a nada, y darle ese contexto al modelo es
    invitarlo a encajar la respuesta en un dominio que nadie pregunto.
    """

    policy = nueva_policy()
    await hasta_la_primera_pregunta(policy)
    assert policy.ultima_pregunta

    policy.marcar_interrumpido(0)
    assert policy.ultima_pregunta == ""


# ==========================================================================
# 2 · El contenido se debe; lo social, no
# ==========================================================================

@pytest.mark.asyncio
async def test_el_acuse_no_oido_no_se_repite() -> None:
    policy = nueva_policy()
    await hasta_la_primera_pregunta(policy)
    # Turno que resuelve nada: la accion sera pedir repetir, sin acuse. Se fuerza a
    # mano una accion con acuse social pendiente.
    accion = policy.ultima_accion
    accion.fragmentos = [
        Fragmento("Aja.", "acuse_1", papel=PAPEL_SOCIAL),
        Fragmento("Entiendo.", "acuse_2", papel=PAPEL_SOCIAL),
    ]

    policy.marcar_interrumpido(0)

    assert policy.deuda == [], "un aja que no se oyo no hay que repetirlo"


@pytest.mark.asyncio
async def test_la_respuesta_del_corpus_cortada_se_debe_y_se_paga() -> None:
    policy = nueva_policy()
    await hasta_la_primera_pregunta(policy)

    accion = policy.ultima_accion
    respuesta = Fragmento("Puede bañarse a partir del quinto dia.", None)
    accion.fragmentos = [respuesta, Fragmento("Y del dolor?", "dolor_reintento",
                                              papel=PAPEL_PREGUNTA, dominio="dolor")]

    policy.marcar_interrumpido(0)
    assert policy.deuda == [respuesta]

    siguiente = await policy.procesar("perdon, siga")
    textos = [f.texto for f in siguiente.fragmentos]

    assert textos[0] == respuesta.texto, "lo pendiente va delante de la respuesta nueva"
    assert policy.deuda == [], "la deuda se paga una vez"


@pytest.mark.asyncio
async def test_la_deuda_no_se_dice_dos_veces() -> None:
    """Si la accion nueva ya contiene lo pendiente, la deuda se cae."""

    policy = nueva_policy()
    await hasta_la_primera_pregunta(policy)

    accion = policy.ultima_accion
    accion.fragmentos = [Fragmento("Repita por favor.", "pedir_repetir")]
    policy.marcar_interrumpido(0)

    siguiente = await policy.procesar("[inaudible] [inaudible]")
    claves = [f.clave for f in siguiente.fragmentos]

    assert claves.count("pedir_repetir") <= 1


@pytest.mark.asyncio
async def test_la_deuda_no_se_cuela_delante_de_una_bandera_roja() -> None:
    """Una pregunta del guion no puede ir delante de una instruccion de urgencia."""

    policy = nueva_policy()
    await hasta_la_primera_pregunta(policy)

    accion = policy.ultima_accion
    accion.fragmentos = [Fragmento("Puede bañarse el viernes.", None)]
    policy.marcar_interrumpido(0)
    assert policy.deuda

    # Un turno que escala: el motor decide rojo y la politica interrumpe el guion.
    policy.estado.fiebre_c.valor = 39.5
    policy.estado.fiebre_c.conocido = True
    siguiente = await policy.procesar("tengo 39 y medio de fiebre")

    assert siguiente.escala_ahora or siguiente.decision.nivel is Nivel.ROJO
    assert "bañarse" not in siguiente.texto_completo
    assert policy.deuda == [], "la deuda se descarta, no se acumula para despues"


# ==========================================================================
# 3 · El registro dice lo que se oyo
# ==========================================================================

@pytest.mark.asyncio
async def test_el_turno_del_agente_queda_truncado_y_marcado() -> None:
    """La hoja que lee una enfermera no puede afirmar una pregunta que se corto."""

    policy = nueva_policy()
    accion = await hasta_la_primera_pregunta(policy)
    planeado = accion.texto_completo

    policy.marcar_interrumpido(1)
    registrado = policy.turnos[-1].texto

    assert registrado.endswith("[interrumpido]")
    assert accion.fragmentos[0].texto in registrado
    assert accion.fragmentos[1].texto not in registrado
    assert len(registrado) < len(planeado) + len(" [interrumpido]")


@pytest.mark.asyncio
async def test_cortado_antes_de_la_primera_silaba() -> None:
    """Interrumpir mientras el agente piensa: no llego a decir nada."""

    policy = nueva_policy()
    await hasta_la_primera_pregunta(policy)

    resultado = policy.marcar_interrumpido(0)

    assert resultado["texto_dicho"] == ""
    assert policy.turnos[-1].texto == "[interrumpido]"
    assert resultado["pregunta_devuelta"] == "dolor"


@pytest.mark.asyncio
async def test_el_resultado_alimenta_el_log_sin_adivinar() -> None:
    policy = nueva_policy()
    await hasta_la_primera_pregunta(policy)
    resultado = policy.marcar_interrumpido(1)

    for campo in ("fragmentos_dichos", "fragmentos_en_deuda", "pregunta_devuelta",
                  "texto_dicho"):
        assert campo in resultado
    assert resultado["fragmentos_dichos"] == 1
    assert resultado["fragmentos_en_deuda"] == 1
