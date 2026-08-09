"""Una locución muda no se guarda en el caché, porque de ahí no sale nunca.

**Lo que se encontró.** Siete locuciones del guion en `data/audio_cache` reducidas a
0.200 s de silencio digital exacto, entre ellas `saludo`, `cierre_verde` y
`cierre_amarillo`. Y con su firma **correcta** en el manifiesto.

Correcta, y ahí está lo venenoso: el manifiesto guarda de qué *texto* salió cada WAV, para
que corregir el guion invalide su audio. El texto no había cambiado, así que la firma
cuadraba, el caché se consideraba vigente, y `pre_renderizar` contaba esas siete como
`reutilizadas`. El sistema habría abierto la llamada con un saludo mudo, y cerrado en verde
con un cierre mudo, sin una línea de log que lo llamara problema. Un `make up` limpio no lo
habría arreglado: el caché es lo que se reutiliza.

**Cómo llegan ahí.** Piper corre como proceso persistente -- se hizo así porque arrancar
uno por frase costaba ~540 ms extra -- y devuelve silencio cuando ese proceso queda en mal
estado. Pasa al matar el servidor a la fuerza mientras sintetiza, que es exactamente lo que
ocurre al reiniciarlo durante el desarrollo.

**Por qué la guarda de antes no servía.** Era `if clave and datos:`. Un WAV de 0.2 s de
silencio son bytes perfectamente no vacíos.
"""

from __future__ import annotations

import io
import json
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from centinela.tts.piper import (  # noqa: E402
    MANIFIESTO,
    PiperTTS,
    silencio,
    tiene_voz,
)


def envolver(muestras: np.ndarray, frecuencia: int = 22050) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(frecuencia)
        w.writeframes(muestras.tobytes())
    return buffer.getvalue()


def tono(segundos: float = 1.0, frecuencia: int = 22050) -> bytes:
    t = np.arange(int(frecuencia * segundos)) / frecuencia
    return envolver((0.3 * 32768 * np.sin(2 * np.pi * 200 * t)).astype(np.int16))


# ==========================================================================
# 1. Reconocer una síntesis muda
# ==========================================================================

def test_el_silencio_exacto_que_se_encontro_no_cuenta_como_voz() -> None:
    """0.200 s: la duración exacta de las siete locuciones destruidas."""

    assert not tiene_voz(envolver(silencio(200)))


@pytest.mark.parametrize("ms", [10, 200, 1000, 15000])
def test_ningun_silencio_cuenta_como_voz(ms: int) -> None:
    assert not tiene_voz(envolver(silencio(ms)))


def test_un_wav_vacio_no_cuenta_como_voz() -> None:
    assert not tiene_voz(b"")
    assert not tiene_voz(envolver(np.zeros(0, dtype=np.int16)))


def test_audio_con_contenido_si_cuenta_como_voz() -> None:
    assert tiene_voz(tono())


def test_una_locucion_de_verdad_del_guion_cuenta_como_voz() -> None:
    """El contraste que impide que la guarda sea demasiado estricta.

    Si `tiene_voz` diera False sobre audio bueno, el cache no se llenaria nunca y cada
    llamada re-sintetizaria las 59 locuciones -- lento y sin sintoma claro.
    """

    cache = Path(__file__).resolve().parents[1] / "data" / "audio_cache"
    wavs = sorted(cache.glob("*.wav"))
    if not wavs:
        pytest.skip("no hay cache de audio en esta copia")

    mudas = [w.name for w in wavs if not tiene_voz(w.read_bytes())]
    assert not mudas, f"hay locuciones mudas en el cache: {mudas}"


# ==========================================================================
# 2. Y no se escribe en el caché
# ==========================================================================

@pytest.mark.asyncio
async def test_una_sintesis_muda_no_se_escribe_ni_se_anota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La comprobacion que importa: ni el WAV ni la firma.

    Que no se anote la firma es la mitad decisiva. Con el WAV mudo en disco y la firma
    correcta, `_vigente` dice que el cache sirve y la locucion no se rehace jamas.
    """

    tts = PiperTTS(dir_cache=tmp_path)

    async def piper_muda(_texto: str) -> bytes:
        return envolver(silencio(200))

    monkeypatch.setattr(tts, "_ejecutar_piper", piper_muda)

    await tts.sintetizar("Buenos dias, le llamo del hospital.", clave="saludo")

    assert not (tmp_path / "saludo.wav").exists(), "se escribio un WAV mudo en el cache"
    assert "saludo" not in tts._manifiesto, (
        "se anoto la firma de una locucion muda: el cache quedaria vigente para siempre"
    )


@pytest.mark.asyncio
async def test_una_sintesis_con_voz_si_se_escribe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tts = PiperTTS(dir_cache=tmp_path)

    async def piper_con_voz(_texto: str) -> bytes:
        return tono()

    monkeypatch.setattr(tts, "_ejecutar_piper", piper_con_voz)

    await tts.sintetizar("Buenos dias.", clave="saludo")

    assert (tmp_path / "saludo.wav").exists()
    assert "saludo" in tts._manifiesto


@pytest.mark.asyncio
async def test_una_muda_no_pisa_una_buena_que_ya_estaba(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El caso real: el saludo estaba bien y una sintesis rota lo dejo mudo."""

    buena = tono(2.0)
    (tmp_path / "saludo.wav").write_bytes(buena)

    tts = PiperTTS(dir_cache=tmp_path)

    async def piper_muda(_texto: str) -> bytes:
        return envolver(silencio(200))

    monkeypatch.setattr(tts, "_ejecutar_piper", piper_muda)

    await tts.sintetizar("Otro texto, que fuerza la sintesis.", clave="saludo")

    assert tiene_voz((tmp_path / "saludo.wav").read_bytes()), (
        "una sintesis muda piso una locucion que estaba bien"
    )


def test_el_manifiesto_del_repo_no_avala_ninguna_locucion_muda() -> None:
    """Guarda sobre el estado real del repo, no sobre un doble.

    Es la forma que tenia el fallo: WAV mudo + firma correcta. Si vuelve a pasar, esto
    lo dice antes de la demo y no durante.
    """

    cache = Path(__file__).resolve().parents[1] / "data" / "audio_cache"
    archivo = cache / MANIFIESTO
    if not archivo.exists():
        pytest.skip("no hay manifiesto en esta copia")

    firmas = json.loads(archivo.read_text(encoding="utf-8"))
    avaladas_y_mudas = [
        clave for clave in firmas
        if (cache / f"{clave}.wav").exists()
        and not tiene_voz((cache / f"{clave}.wav").read_bytes())
    ]
    assert not avaladas_y_mudas, (
        f"el manifiesto da por buenas locuciones mudas: {avaladas_y_mudas}"
    )
