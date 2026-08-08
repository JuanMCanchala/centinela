"""Que el texto que entra al fonemizador se lea como lo leeria una persona.

Cada caso de aqui salio de fonemizar con `piper --debug` el texto que el agente dice
hoy y encontrar que sonaba a maquina. Los dos que importan de verdad:

  - "llame al 123" se pronunciaba "ciento veintitres". Es la linea de emergencias del
    cierre rojo: dicha asi, el paciente no reconoce el numero.
  - "fiebre de 38.5" se pronunciaba "treinta y ocho punto cinco".

Y una garantia que no es de estilo: `para_voz` no toca el registro clinico. Se aplica
en la capa de voz, asi que la hoja de traspaso sigue diciendo la cifra.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from centinela.tts.hablado import para_voz  # noqa: E402


# ---------------------------------------------------------------- la linea de urgencias

def test_la_linea_de_emergencias_se_dice_digito_a_digito() -> None:
    assert "uno dos tres" in para_voz("llame al 123 si no tiene como desplazarse")


def test_un_123_que_no_es_telefono_sigue_siendo_cantidad() -> None:
    """Sin el precedente que lo declara telefono, 123 es un numero y se lee como tal."""

    assert "uno dos tres" not in para_voz("el corpus tiene 123 documentos")


# ---------------------------------------------------------------- decimales

def test_el_medio_grado_se_dice_y_medio() -> None:
    assert para_voz("fiebre de 38.5 grados") == "fiebre de 38 y medio grados"


def test_la_coma_decimal_tambien() -> None:
    assert para_voz("fiebre de 38,5 grados") == "fiebre de 38 y medio grados"


def test_otro_decimal_se_dice_con() -> None:
    assert para_voz("temperatura de 37.2") == "temperatura de 37 con 2"


def test_el_punto_cero_no_se_dice() -> None:
    """"Treinta y ocho punto cero" no informa nada que "treinta y ocho" no diga."""

    assert para_voz("fiebre de 38.0 grados") == "fiebre de 38 grados"


# ---------------------------------------------------------------- unidades y rangos

def test_los_grados_centigrados() -> None:
    assert para_voz("38.5 °C") == "38 y medio grados"


def test_un_rango_se_lee_como_rango() -> None:
    assert para_voz("entre 38-39 grados") == "entre 38 a 39 grados"


def test_la_escala_de_dolor() -> None:
    assert para_voz("un dolor de 7/10") == "un dolor de 7 sobre 10"


def test_unidades_de_dosis() -> None:
    assert para_voz("500mg cada 8 horas") == "500 miligramos cada 8 horas"


# ---------------------------------------------------------------- lo que no debe pasar

def test_el_texto_sin_cifras_no_se_toca() -> None:
    frase = "Lo que me acaba de describir necesita atención médica ahora, no mañana."

    assert para_voz(frase) == frase


def test_las_tildes_sobreviven() -> None:
    """La correccion ortografica y esta capa tienen que poder convivir."""

    assert "diríjase" in para_voz("diríjase al servicio de urgencias, o llame al 123")


def test_texto_vacio() -> None:
    assert para_voz("") == ""


# ==========================================================================
# Lo que una cita del corpus NO puede decir en voz alta
# ==========================================================================

from centinela.rag.answerer import (  # noqa: E402
    es_relleno_de_formulario,
    nombre_pronunciable,
)


def test_un_hueco_de_formulario_no_es_una_respuesta_hablable() -> None:
    """El caso real: la unica cita que el folleto de mastectomia dejaba pasar.

    Leido por telefono, "restriccion de amplitud de movimiento raya raya raya grados"
    es peor que un "no lo se".
    """

    assert es_relleno_de_formulario("Restricción de amplitud de movimiento: _____ grados")
    assert es_relleno_de_formulario("Fecha de control ......... hora")
    assert not es_relleno_de_formulario("Camine 10 minutos tres veces al día.")


def test_la_fuente_se_nombra_por_su_titulo_y_no_por_el_archivo() -> None:
    """`mskcc_ejercicios_tras_mastectomia.pdf` dicho en voz alta son guiones bajos."""

    class Doc:
        titulo = "Ejercicios para hacer después de su mastectomía"

    dicho = nombre_pronunciable(Doc(), "mskcc_ejercicios_tras_mastectomia.pdf")

    assert dicho == "Ejercicios para hacer después de su mastectomía"
    assert "_" not in dicho and ".pdf" not in dicho


def test_sin_titulo_registrado_el_archivo_se_hace_legible() -> None:
    class SinTitulo:
        titulo = ""

    dicho = nombre_pronunciable(SinTitulo(), "002-GUIA-DE-CANCER.pdf")

    assert "_" not in dicho and ".pdf" not in dicho
    assert dicho == "002 GUIA DE CANCER"


def test_sin_documento_tampoco_se_lee_la_extension() -> None:
    """`obtener_documento` puede devolver None si el doc se olvido entre medias."""

    assert nombre_pronunciable(None, "guia_postoperatoria.pdf") == "guia postoperatoria"
