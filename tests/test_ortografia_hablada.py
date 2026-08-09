"""El guion se escribe en español real porque el fonemizador lee la ortografia.

Piper fonemiza con espeak-ng, que deduce la silaba tonica de como esta escrita la
palabra. Este guion estuvo escrito sin tildes y sin ñ, y lo que decia en voz alta,
comprobado con `piper --debug` sobre `es_MX-ald-medium`:

    sueno      ->  swˈeno      (no es una palabra)
    manana     ->  manˈana     (no es una palabra)
    clinico    ->  klinˈiko    en vez de klˈiniko
    atencion   ->  atˈɛnsjon   en vez de atensjˈon
    medica     ->  meðˈika     el verbo, no el adjetivo
    dirijase   ->  diɾixˈase   en vez de diɾˈixase

Las tres ultimas estaban en `CIERRE_ROJO`, que es la frase que manda al paciente a
urgencias. Este archivo es la red que impide que vuelvan a entrar.

Nota sobre `¿`: se comprobo que espeak-ng NO cambia los fonemas por el signo de
apertura -- la misma frase con y sin `¿` da `le paɾˈese βjˈen?` en los dos casos. Se
exige de todas formas, porque el texto de la locucion tambien se muestra en la consola
y se guarda en el registro de la llamada.
"""

from __future__ import annotations

import re
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from centinela.dialog import script as S  # noqa: E402

# Formas sin tilde ni ñ que estuvieron en el guion. Cada una se pronunciaba mal.
ROTAS = (
    "sueno", "manana", "clinico", "clinica", "medica", "medico", "atencion",
    "cirugia", "recuperacion", "informacion", "escalofrios", "termometro",
    "liquido", "liquidos", "hinchazon", "numero", "ultimo", "dias", "dia",
    "aqui", "asi", "entendi", "escuche", "deje", "conto", "apunte", "dirijase",
    "cuenteme", "digame", "permitame", "pondria", "seria", "cual", "como",
    "oir", "ojala", "esta bien", "diagnostico", "que color", "en que",
    # Estas se añadieron despues: vivian en los f-strings de `dialog/policy.py`, que el
    # test de locuciones no alcanza. Por eso existe el recorrido de llamada de abajo.
    "despues", "centigrados", "linea", "pidio", "mas de", "telefono",
)

_RE_PALABRA = re.compile(r"[a-záéíóúüñ]+", re.IGNORECASE)


def _todas() -> list[S.Locucion]:
    extra = list(S.naturalidad()) + [S.INTERRUPCION_ROJA, S.RETOMAR_URGENCIA]
    return S.todas_las_locuciones() + extra


def test_ninguna_locucion_lleva_una_palabra_sin_tilde() -> None:
    culpables = []

    for loc in _todas():
        palabras = {p.lower() for p in _RE_PALABRA.findall(loc.texto)}
        for rota in ROTAS:
            # Las de una palabra se comparan como palabra completa; las de dos, como
            # subcadena, porque "esta bien" solo esta mal en ese contexto.
            fallo = rota in palabras if " " not in rota else rota in loc.texto.lower()
            if fallo:
                culpables.append(f"{loc.clave}: {rota!r}")

    assert not culpables, "texto que el TTS pronunciara mal:\n" + "\n".join(culpables)


def test_toda_pregunta_abre_con_el_signo() -> None:
    """Cada `?` tiene que tener su `¿` sin otro `?` en medio."""

    culpables = []

    for loc in _todas():
        for pos in (m.start() for m in re.finditer(r"\?", loc.texto)):
            previo = loc.texto[:pos]
            abre = previo.rfind("¿")
            cierra = previo.rfind("?")
            if abre == -1 or abre < cierra:
                culpables.append(f"{loc.clave}: ...{loc.texto[max(0, pos - 40):pos + 1]}")

    assert not culpables, "pregunta sin signo de apertura:\n" + "\n".join(culpables)


def test_la_ene_con_virgulilla_esta_donde_toca() -> None:
    """Los dos casos donde faltaba la ñ, uno de ellos en las seis preguntas de sueño."""

    assert "sueño" in S.PREGUNTA_POR_DOMINIO["sueno"].reintento.texto
    assert "mañana" in S.CIERRE_ROJO.texto


def test_la_instruccion_de_urgencias_se_dice_con_enfasis() -> None:
    """Es la unica frase cuya perdida es daño clinico, y la unica marcada."""

    con_enfasis = [l.clave for l in S.todas_las_locuciones() if l.enfasis]

    assert con_enfasis == [S.CIERRE_ROJO.clave]


def test_la_linea_de_emergencias_sigue_en_el_cierre_rojo() -> None:
    """La cifra se conserva en el texto; `tts/hablado.py` la dice digito a digito."""

    from centinela.tts.hablado import para_voz

    assert "123" in S.CIERRE_ROJO.texto
    assert "uno dos tres" in para_voz(S.CIERRE_ROJO.texto)


# ==========================================================================
# El anuncio de la bandera roja, en el idioma del paciente
# ==========================================================================

def test_el_anuncio_de_la_bandera_no_lee_el_enunciado_del_umbral() -> None:
    """Le decia al paciente "fiebre igual o mayor a 38.0 grados centigrados".

    Ese texto esta escrito para el registro clinico y para la enfermera que lo revisa.
    Y era incoherente: en el turno anterior el agente acaba de leer de vuelta "me dice
    fiebre de 38.5 grados", asi que decia lo mismo de dos formas seguidas.
    """

    from centinela.clinical.triage_engine import TriageEngine
    from centinela.dialog.confirmacion import hallazgos_como_al_hablar
    from centinela.models import ClinicalState

    estado = ClinicalState()
    estado.fiebre_c.valor = 38.5
    estado.fiebre_c.conocido = True
    decision = TriageEngine().evaluar(estado, cerrar=False)

    dicho = hallazgos_como_al_hablar(estado, decision.reglas_rojas)

    assert dicho == "fiebre de 38.5 grados"
    assert "igual o mayor" not in dicho
    assert "centigrados" not in dicho


def test_un_hallazgo_sin_forma_hablada_conserva_su_descripcion() -> None:
    """Sonar mejor no puede costar informacion clinica."""

    from centinela.dialog.confirmacion import hallazgos_como_al_hablar
    from centinela.models import ClinicalState

    class ReglaRara:
        dominio = "dominio_que_no_existe"
        descripcion = "Hallazgo Sin Frase Hablada"
        valor_observado = None

    assert hallazgos_como_al_hablar(ClinicalState(), [ReglaRara()]) == "hallazgo sin frase hablada"


# ==========================================================================
# La red sobre el habla REAL, no solo sobre las locuciones fijas
#
# Los tests de arriba miran `dialog/script.py`. Pero la politica tambien construye
# texto al vuelo con f-strings, y ahi se habia quedado un "despues de una
# colecistectomia" que ningun test de locuciones podia ver. Esto recorre una llamada
# completa por la maquina de estados y revisa todo lo que el agente dice.
# ==========================================================================

@pytest.mark.asyncio
async def test_nada_de_lo_que_dice_el_agente_en_una_llamada_esta_sin_tildes() -> None:
    from centinela.clinical.extractor import ResultadoExtraccion
    from centinela.clinical.normalizer import normalizar_turno
    from centinela.clinical.triage_engine import TriageEngine
    from centinela.dialog.policy import DialogPolicy, Paciente

    class ExtractorConFiebre:
        async def extraer(self, texto_paciente, estado, turno_idx, pregunta_agente="",
                          dominio_objetivo="", **_):
            norm = normalizar_turno(texto_paciente, dominio_objetivo)
            if "fiebre" in texto_paciente.lower():
                estado.fiebre_c.valor = 38.5
                estado.fiebre_c.conocido = True
            return ResultadoExtraccion(estado=estado, normalizado=norm, respondio=True)

    dicho: list[str] = []

    # Dos recorridos: uno que llega a la bandera roja (donde vive el anuncio construido
    # al vuelo) y otro que atraviesa los seis dominios hasta el cierre.
    for guion in (
        ["Si, soy yo", "he tenido fiebre", "si, es correcto"],
        ["Si, soy yo", "un dolor de dos", "no he tenido nada de eso", "camino normal",
         "la herida se ve bien", "como normal", "duermo bien"],
    ):
        policy = DialogPolicy(
            paciente=Paciente(paciente_id="pac", nombre="Paciente Test",
                              procedimiento="Colecistectomía", dia_postop=3),
            extractor=ExtractorConFiebre(),
            motor=TriageEngine(),
        )
        dicho.append(policy.abrir().texto_completo)
        for turno in guion:
            dicho.append((await policy.procesar(turno)).texto_completo)

    culpables = []
    for texto in dicho:
        palabras = {p.lower() for p in _RE_PALABRA.findall(texto)}
        for rota in ROTAS:
            fallo = rota in palabras if " " not in rota else rota in texto.lower()
            if fallo:
                culpables.append(f"{rota!r} en: ...{texto[:90]}")

    assert dicho, "el recorrido no produjo habla, el test no estaria probando nada"
    assert not culpables, "el agente pronunciara mal:\n" + "\n".join(culpables)
