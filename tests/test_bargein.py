"""El detector de interrupcion.

Lo que se prueba aqui no es que la maquina de estados transite bien -- eso es
inspeccionable leyendo el modulo --, sino las cuatro propiedades de las que depende
que la llamada se sienta como una llamada:

  1. **El umbral sigue al eco.** El mismo nivel de voz interrumpe con auriculares y
     no interrumpe con el altavoz a todo volumen, sin tocar una constante.
  2. **El detector no se ciega a si mismo.** Cuanto mas alto habla el paciente, mas
     facil tiene que ser interrumpir, no mas dificil. Si la voz del paciente
     entrara en la ventana de eco, ocurriria lo contrario.
  3. **Un pico no corta.** Un golpe, una silla, una silaba fuerte del eco.
  4. **Un falso positivo converge.** Descartar una sospecha ensena al detector, asi
     que un cambio de volumen del sistema cuesta un bache y no uno por silaba.

Las tramas son de 64 ms porque es lo que produce el cliente a 16 kHz con buffer de
1024 muestras (`web/app.js`, `VAD.msPorTrama`).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from centinela.stt.bargein import (  # noqa: E402
    FACTOR_SOBRE_VOZ,
    UMBRAL_MINIMO,
    DetectorInterrupcion,
    Veredicto,
    rms_de,
)

MS = 64.0


def alimentar(det: DetectorInterrupcion, rms: float, tramas: int) -> list[Veredicto]:
    return [det.observar(rms, MS) for _ in range(tramas)]


def pasar_la_gracia(det: DetectorInterrupcion, eco: float = 0.0) -> None:
    """Consume la ventana de gracia aprendiendo `eco` como suelo."""

    alimentar(det, eco, 6)


# ==========================================================================
# Mientras el agente no tiene la palabra, este detector no opina
# ==========================================================================

def test_en_reposo_no_opina() -> None:
    """Con el agente callado, quien abre turno es el VAD del cliente.

    Si el detector opinara aqui, habria dos cosas decidiendo lo mismo.
    """

    det = DetectorInterrupcion()
    assert all(v is Veredicto.NADA for v in alimentar(det, 0.9, 20))
    assert det.sospechas == 0


def test_soltar_la_palabra_vuelve_al_reposo() -> None:
    det = DetectorInterrupcion()
    det.tomar_la_palabra()
    det.soltar_la_palabra()
    assert not det.escuchando_el_suelo
    assert all(v is Veredicto.NADA for v in alimentar(det, 0.9, 10))


# ==========================================================================
# Ventana de gracia
# ==========================================================================

def test_la_gracia_no_sospecha_pero_aprende() -> None:
    """El eco tarda en llegar al microfono; el primer cuarto de segundo solo mide."""

    det = DetectorInterrupcion()
    det.tomar_la_palabra()

    # 250 ms de eco fuerte. Sin la gracia, esto disparia en la segunda trama.
    veredictos = alimentar(det, 0.20, 3)

    assert all(v is Veredicto.NADA for v in veredictos)
    assert det.eco > 0.0, "la gracia tiene que servir para aprender, no solo para callar"


def test_pasada_la_gracia_si_sospecha() -> None:
    det = DetectorInterrupcion()
    det.tomar_la_palabra()
    pasar_la_gracia(det, eco=0.005)

    primera = det.observar(0.30, MS)
    segunda = det.observar(0.30, MS)

    assert primera is Veredicto.NADA, "una sola trama no basta"
    assert segunda is Veredicto.SOSPECHA
    assert det.sospechas == 1


def test_pensando_no_hay_gracia() -> None:
    """Mientras el agente piensa no sale audio, asi que no hay eco que esperar.

    Es el caso de "tarda mucho y el paciente mete baza". Ahi la interrupcion tiene
    que ser inmediata.
    """

    det = DetectorInterrupcion()
    det.tomar_la_palabra(emite_voz=False)

    primera = det.observar(0.30, MS)
    segunda = det.observar(0.30, MS)

    assert primera is Veredicto.NADA
    assert segunda is Veredicto.SOSPECHA


def test_pensando_olvida_el_eco_de_la_locucion_anterior() -> None:
    """Si conservara el eco, el umbral seguiria alto sin altavoz sonando.

    El paciente no podria meter baza justo cuando mas ganas tiene: cuando el agente
    se esta tomando su tiempo.
    """

    det = DetectorInterrupcion()
    det.tomar_la_palabra(emite_voz=True)
    pasar_la_gracia(det, eco=0.15)
    umbral_con_altavoz = det.umbral

    det.tomar_la_palabra(emite_voz=False)

    assert umbral_con_altavoz > 0.2
    assert det.eco == 0.0
    assert det.umbral < umbral_con_altavoz


# ==========================================================================
# El umbral sigue al eco
# ==========================================================================

def test_con_auriculares_basta_hablar_normal() -> None:
    """Eco practicamente nulo: el umbral baja al suelo y no por debajo."""

    det = DetectorInterrupcion(umbral_voz=0.022)
    det.tomar_la_palabra()
    pasar_la_gracia(det, eco=0.001)

    assert det.umbral == max(0.022 * FACTOR_SOBRE_VOZ, UMBRAL_MINIMO)
    alimentar(det, 0.06, 2)
    assert det.sospechas == 1, "con auriculares una voz normal tiene que cortar"


def test_con_altavoz_alto_hay_que_hablar_mas_alto() -> None:
    """Mismo nivel de voz, distinta sala: el detector se adapta solo."""

    flojo = DetectorInterrupcion()
    flojo.tomar_la_palabra()
    pasar_la_gracia(flojo, eco=0.010)

    fuerte = DetectorInterrupcion()
    fuerte.tomar_la_palabra()
    pasar_la_gracia(fuerte, eco=0.120)

    assert fuerte.umbral > flojo.umbral * 3

    # Una voz de 0.09 corta en la sala con auriculares y no en la del altavoz.
    alimentar(flojo, 0.09, 3)
    alimentar(fuerte, 0.09, 3)

    assert flojo.sospechas == 1
    assert fuerte.sospechas == 0


def test_el_umbral_nunca_baja_del_suelo() -> None:
    det = DetectorInterrupcion(umbral_voz=0.0)
    det.tomar_la_palabra()
    pasar_la_gracia(det, eco=0.0)
    assert det.umbral == 0.030


def test_el_eco_es_un_percentil_no_el_maximo() -> None:
    """Un golpe aislado durante la locucion no sube el umbral toda la frase."""

    det = DetectorInterrupcion()
    det.tomar_la_palabra()
    pasar_la_gracia(det, eco=0.01)
    alimentar(det, 0.01, 20)

    umbral_tranquilo = det.umbral
    # Una trama suelta muy alta, por debajo del umbral para que se aprenda.
    det.observar(umbral_tranquilo * 0.99, MS)

    assert det.umbral == umbral_tranquilo, "un solo valor no puede mover el p95"


# ==========================================================================
# El detector no se ciega a si mismo
# ==========================================================================

def test_la_voz_del_paciente_no_entra_en_la_ventana_de_eco() -> None:
    """La propiedad que impide el fallo mas perverso posible.

    Si las tramas por encima del umbral alimentaran la ventana, hablar mas alto
    subiria el umbral, y el paciente que grita para hacerse oir seria justo el que
    no consigue interrumpir.
    """

    det = DetectorInterrupcion()
    det.tomar_la_palabra()
    pasar_la_gracia(det, eco=0.01)
    eco_antes = det.eco

    # Voz sostenida muy por encima del umbral durante casi un segundo.
    alimentar(det, 0.5, 14)

    assert det.eco == eco_antes, "la voz del paciente contamino el suelo de eco"


def test_hablar_mas_alto_siempre_interrumpe_antes() -> None:
    suave = DetectorInterrupcion()
    suave.tomar_la_palabra()
    pasar_la_gracia(suave, eco=0.02)

    fuerte = DetectorInterrupcion()
    fuerte.tomar_la_palabra()
    pasar_la_gracia(fuerte, eco=0.02)

    alimentar(suave, 0.055, 4)
    alimentar(fuerte, 0.400, 4)

    assert fuerte.sospechas >= suave.sospechas


# ==========================================================================
# Un pico no corta
# ==========================================================================

def test_una_trama_aislada_no_sospecha() -> None:
    """Un golpe en la mesa: 64 ms de energia y vuelta al silencio."""

    det = DetectorInterrupcion()
    det.tomar_la_palabra()
    pasar_la_gracia(det, eco=0.01)

    for _ in range(6):
        det.observar(0.40, MS)   # el golpe
        det.observar(0.01, MS)   # silencio otra vez

    assert det.sospechas == 0


def test_la_racha_se_rompe_con_una_trama_por_debajo() -> None:
    det = DetectorInterrupcion(tramas_para_sospecha=3)
    det.tomar_la_palabra()
    pasar_la_gracia(det, eco=0.01)

    det.observar(0.40, MS)
    det.observar(0.40, MS)
    det.observar(0.01, MS)
    det.observar(0.40, MS)
    det.observar(0.40, MS)

    assert det.sospechas == 0, "la racha tiene que ser seguida"


# ==========================================================================
# La ventana de confirmacion
# ==========================================================================

def test_comprobar_llega_a_los_250_ms_y_no_antes() -> None:
    det = DetectorInterrupcion()
    det.tomar_la_palabra()
    pasar_la_gracia(det, eco=0.01)
    alimentar(det, 0.40, 2)
    assert det.sospechando

    # 64, 128, 192 ms: todavia se esta acumulando audio para el STT.
    assert det.observar(0.40, MS) is Veredicto.NADA
    assert det.observar(0.40, MS) is Veredicto.NADA
    assert det.observar(0.40, MS) is Veredicto.NADA
    # 256 ms: ya hay material suficiente para preguntarle si eso era voz.
    assert det.observar(0.40, MS) is Veredicto.COMPROBAR


def test_confirmar_suelta_la_palabra() -> None:
    det = DetectorInterrupcion()
    det.tomar_la_palabra()
    pasar_la_gracia(det, eco=0.01)
    alimentar(det, 0.40, 6)
    det.confirmado()

    assert not det.escuchando_el_suelo
    assert det.confirmadas == 1
    assert all(v is Veredicto.NADA for v in alimentar(det, 0.9, 5))


def test_descartar_devuelve_la_voz_al_agente() -> None:
    det = DetectorInterrupcion()
    det.tomar_la_palabra()
    pasar_la_gracia(det, eco=0.01)
    alimentar(det, 0.40, 6)
    det.descartado()

    assert det.escuchando_el_suelo
    assert not det.sospechando
    assert det.descartadas == 1


def test_un_falso_positivo_ensena_al_detector() -> None:
    """La propiedad que hace converger esto.

    Sin ella, una locucion con silabas fuertes produciria un bache por silaba: cada
    pico dispara, se descarta, y el siguiente pico vuelve a disparar porque el
    umbral no se movio. Al descartar se aprende que ese nivel era eco.
    """

    det = DetectorInterrupcion()
    det.tomar_la_palabra()
    pasar_la_gracia(det, eco=0.01)

    umbral_antes = det.umbral
    alimentar(det, 0.20, 6)      # silaba fuerte del eco
    det.descartado()

    assert det.umbral > umbral_antes
    assert det.umbral > 0.20, "el nivel que resulto ser eco tiene que quedar por debajo"

    # Y ahora la misma silaba ya no molesta.
    pasar_la_gracia(det, eco=0.01)
    alimentar(det, 0.20, 6)
    assert det.sospechas == 1, "el segundo pico del mismo eco no puede volver a cortar"


def test_confirmar_no_ensena_nada() -> None:
    """Lo que resulto ser voz no es eco, y no puede subir el suelo."""

    det = DetectorInterrupcion()
    det.tomar_la_palabra()
    pasar_la_gracia(det, eco=0.01)
    umbral_antes = det.umbral

    alimentar(det, 0.60, 6)
    det.confirmado()
    det.tomar_la_palabra()

    assert det.umbral == umbral_antes


# ==========================================================================
# Lo que el runbook promete que queda en el log
# ==========================================================================

def test_la_instantanea_explica_el_corte() -> None:
    """`docs/operacion.md` promete diagnosticar "me corta solo" con estos campos."""

    det = DetectorInterrupcion()
    det.tomar_la_palabra()
    pasar_la_gracia(det, eco=0.02)
    alimentar(det, 0.40, 2)

    foto = det.instantanea()
    for campo in ("fase", "rms_disparo", "umbral", "eco_p95", "sospechas"):
        assert campo in foto, f"{campo} hace falta para diagnosticar sin adivinar"
    assert foto["rms_disparo"] > foto["eco_p95"]


# ==========================================================================
# El nivel se calcula igual aqui y en el arnes
# ==========================================================================

def test_el_punto_de_operacion_es_el_que_midio_el_barrido() -> None:
    """Los dos numeros que deciden salieron de una medicion, no de un criterio.

    `make bargein --barrido` publica la tabla: con (85, 1.8) la deteccion es del 100 %
    mientras el eco quede 12 dB por debajo de la voz, y del 83 % con el altavoz al
    doble de eso, a cambio de 0.23 baches por minuto de habla del agente. Subir a
    (85, 2.2) los deja en cero pero pierde la mitad de la deteccion con altavoz alto;
    bajar a (75, 1.8) cuadruplica los baches para ganar poco.

    Si alguien mueve estas constantes, este test le recuerda que hay una tabla que
    volver a correr.
    """

    det = DetectorInterrupcion()
    assert det.percentil_eco == 85.0
    assert det.margen_eco == 1.8


def test_la_configuracion_no_puede_separarse_del_punto_medido() -> None:
    """El servidor pasa `config.bargein_margen_eco` al detector, asi que si los dos
    valores por defecto se separan, lo que corre no es lo que se midio.

    Es exactamente lo que paso: el modulo bajo a 1.8 tras el barrido y `config.py` se
    quedo en 2.2, asi que el detector recibia 2.2 en cada llamada y la tabla publicada
    describia otro sistema. Este test lo hace imposible.
    """

    from centinela.config import config
    from centinela.stt.bargein import MARGEN_ECO, MS_CONFIRMACION

    assert config.bargein_margen_eco == MARGEN_ECO
    assert config.bargein_ms_confirmacion == MS_CONFIRMACION


def test_rms_de_bloque_vacio() -> None:
    assert rms_de(np.array([], dtype=np.float32)) == 0.0


def test_rms_de_senoidal_conocida() -> None:
    """El RMS de una senoidal de amplitud A es A/raiz(2)."""

    t = np.arange(0, 16000, dtype=np.float32) / 16000.0
    onda = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    assert abs(rms_de(onda) - 0.5 / np.sqrt(2)) < 1e-3


def test_rms_de_silencio_y_de_continua() -> None:
    assert rms_de(np.zeros(1024, dtype=np.float32)) == 0.0
    assert abs(rms_de(np.full(1024, 0.25, dtype=np.float32)) - 0.25) < 1e-6
