"""El tratamiento del audio que sale del motor, y por que existe cada pieza.

Las dos medidas que motivaron esto, tomadas sobre las 59 locuciones ya renderizadas:

  - la cola de silencio de Piper iba de **200 a 785 ms** segun la locucion, asi que el
    hueco entre dos fragmentos de un mismo turno cambiaba de duracion sin motivo. Esa
    cadencia erratica es lo que suena a maquina, mas que el timbre.
  - las 59 estaban a **pico 1.0** (0 dBFS, sin headroom) mientras el RMS de voz variaba
    **4.2 dB** entre ellas: el volumen percibido saltaba de frase en frase.
"""

from __future__ import annotations

import io
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from centinela.tts import piper as P  # noqa: E402

FREC = P.FRECUENCIA_OBJETIVO


def _wav(muestras: np.ndarray, frecuencia: int = FREC) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(frecuencia)
        w.writeframes(muestras.astype(np.int16).tobytes())
    return buffer.getvalue()


def _duracion_ms(wav: bytes) -> float:
    datos, frecuencia = P._muestras(wav)
    return datos.size / frecuencia * 1000


def _tono(ms: int, amplitud: float = 0.5) -> np.ndarray:
    n = int(FREC * ms / 1000)
    t = np.arange(n) / FREC
    return (np.sin(2 * np.pi * 220 * t) * amplitud * 32767).astype(np.int16)


def _silencio(ms: int) -> np.ndarray:
    return np.zeros(int(FREC * ms / 1000), dtype=np.int16)


# ---------------------------------------------------------------- recorte

def test_la_cola_larga_queda_en_una_duracion_conocida() -> None:
    """Es el caso real: 785 ms de cola en una locucion y 200 en la siguiente."""

    largo = _wav(np.concatenate([_silencio(60), _tono(500), _silencio(785)]))
    corto = _wav(np.concatenate([_silencio(60), _tono(500), _silencio(200)]))

    a, b = P.recortar_silencio(largo), P.recortar_silencio(corto)

    assert abs(_duracion_ms(a) - _duracion_ms(b)) < 5, "las colas deben quedar iguales"


def test_el_recorte_deja_margen_y_no_corta_la_voz() -> None:
    """Cortar en la primera muestra audible produce un chasquido al arrancar."""

    wav = P.recortar_silencio(_wav(np.concatenate([_silencio(300), _tono(500), _silencio(300)])))
    duracion = _duracion_ms(wav)

    assert duracion > 500, "no puede comerse la voz"
    assert duracion < 500 + P.DEJAR_MS_INICIAL + P.DEJAR_MS_FINAL + 10


def test_un_wav_todo_silencio_no_desaparece() -> None:
    """Sin muestras audibles no hay donde recortar, y devolverlo vacio seria peor."""

    entrada = _wav(_silencio(200))

    assert P.recortar_silencio(entrada) == entrada


def test_un_wav_vacio_no_revienta() -> None:
    assert P.recortar_silencio(b"") == b""
    assert P.normalizar_nivel(b"") == b""


# ---------------------------------------------------------------- nivel

def test_dos_locuciones_de_volumen_distinto_acaban_igual() -> None:
    """El defecto medido: 4.2 dB de diferencia de RMS entre locuciones del guion."""

    def rms(wav: bytes) -> float:
        datos, _ = P._muestras(wav)
        flotante = datos.astype(np.float32) / 32768.0
        con_voz = flotante[np.abs(flotante) > P.UMBRAL_SILENCIO]
        return float(np.sqrt(np.mean(con_voz**2)))

    fuerte = P.normalizar_nivel(_wav(_tono(400, amplitud=0.9)))
    floja = P.normalizar_nivel(_wav(_tono(400, amplitud=0.2)))

    assert abs(rms(fuerte) - rms(floja)) < 0.01


def _como_la_voz(ms: int, rms_deseado: float) -> np.ndarray:
    """Una señal con el factor de cresta de esta voz: pico 1.0 y RMS bajo.

    Hace falta porque un seno tiene factor de cresta 1.41 y la voz de Piper lo tiene
    entre 3.6 y 5.8. Sobre un seno, cualquier normalizador parece roto.
    """

    n = int(FREC * ms / 1000)
    t = np.arange(n) / FREC
    onda = np.sin(2 * np.pi * 220 * t)
    onda = onda * (0.35 + 0.65 * np.abs(np.sin(2 * np.pi * 3.5 * t)))
    onda = onda / np.sqrt(np.mean(onda**2)) * rms_deseado
    onda[n // 2] = 1.0  # el transitorio de consonante que se lleva el pico
    return (onda * 32767).astype(np.int16)


def test_queda_headroom_bajo_el_techo() -> None:
    """Piper entrega todo a pico 1.0. Eso es cero margen y riesgo de recorte."""

    datos, _ = P._muestras(P.normalizar_nivel(_wav(_como_la_voz(400, 0.20))))
    pico = float(np.max(np.abs(datos))) / 32768

    assert pico <= 1.0
    assert pico > 0.5, "tampoco puede quedarse inaudible"


def test_el_transitorio_aislado_no_aplasta_la_locucion_entera() -> None:
    """El fallo que el maximo absoluto causaba y el percentil resuelve.

    Las 59 locuciones tienen pico exactamente 1.000. Midiendo el techo con el maximo,
    todas recibian la misma reduccion y el RMS seguia variando los mismos 4.1 dB: la
    normalizacion no igualaba nada.
    """

    def rms(wav: bytes) -> float:
        datos, _ = P._muestras(wav)
        flotante = datos.astype(np.float32) / 32768.0
        voz = flotante[np.abs(flotante) > P.UMBRAL_SILENCIO]
        return float(np.sqrt(np.mean(voz**2)))

    floja = P.normalizar_nivel(_wav(_como_la_voz(400, 0.17)))
    fuerte = P.normalizar_nivel(_wav(_como_la_voz(400, 0.28)))

    assert abs(rms(floja) - rms(fuerte)) < 0.01, "las dos tenian pico 1.0 y RMS distinto"


def test_el_nivel_se_mide_sobre_la_voz_y_no_sobre_los_silencios() -> None:
    """Si no, una locucion con pausas largas se escalaria mas que una seguida."""

    seguida = _wav(_tono(400, amplitud=0.5))
    con_pausas = _wav(np.concatenate([_tono(130, 0.5), _silencio(400), _tono(130, 0.5)]))

    def pico(wav: bytes) -> float:
        datos, _ = P._muestras(wav)
        return float(np.max(np.abs(datos))) / 32768

    assert abs(pico(P.normalizar_nivel(seguida)) - pico(P.normalizar_nivel(con_pausas))) < 0.02


# ---------------------------------------------------------------- cadencia

def test_la_pausa_entre_fragmentos_es_la_declarada() -> None:
    uno, dos = _wav(_tono(300)), _wav(_tono(300))

    junto = P.concatenar_wav([uno, dos], pausa_ms=90)

    assert abs(_duracion_ms(junto) - (300 + 90 + 300)) < 5


def test_un_solo_fragmento_no_gana_pausa() -> None:
    uno = _wav(_tono(300))

    assert P.concatenar_wav([uno]) == uno


def test_los_trozos_vacios_no_cuentan_como_fragmento() -> None:
    """Si contaran, un fragmento sin audio metaria una pausa que nadie pidio."""

    uno = _wav(_tono(300))

    assert P.concatenar_wav([uno, b"", None]) == uno


# ---------------------------------------------------------------- invalidacion

def test_cambiar_el_texto_invalida_el_audio_cacheado(tmp_path) -> None:
    """El defecto de raiz: la clave nombra el archivo, no su contenido.

    Sin esto, corregir la ortografia de una locucion no cambiaba nada de lo que el
    paciente oia: el WAV viejo seguia en disco y se servia igual.
    """

    tts = P.PiperTTS.__new__(P.PiperTTS)
    tts.dir_cache = tmp_path
    tts._firmas = None
    tts._firmas_sucias = False

    tts._anotar("cierre_rojo", "necesita atencion medica")

    assert tts._vigente("cierre_rojo", "necesita atencion medica") is True
    assert tts._vigente("cierre_rojo", "necesita atención médica") is False


def test_el_perfil_de_enfasis_tiene_su_propia_firma() -> None:
    """El mismo texto dicho mas despacio es otro audio."""

    tts = P.PiperTTS.__new__(P.PiperTTS)

    assert tts._firma("diríjase a urgencias") != tts._firma("diríjase a urgencias", enfasis=True)


def test_una_clave_desconocida_no_esta_vigente(tmp_path) -> None:
    tts = P.PiperTTS.__new__(P.PiperTTS)
    tts.dir_cache = tmp_path
    tts._firmas = None

    assert tts._vigente("nunca_vista", "hola") is False
