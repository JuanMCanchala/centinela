"""Compara Kokoro con Piper sobre las frases reales del guion, y deja las muestras.

Por que Kokoro. El catalogo de Piper en espanol tiene **nueve voces**, y de ellas una sola
es femenina y latinoamericana: `es_AR-daniela-high`. Cuando el criterio pasa a ser el timbre
--y lo es, porque cinco de las seis voces de Piper cuestan lo mismo en latencia-- nueve
opciones son pocas, y ninguna es colombiana.

Kokoro-82M es de otra clase de modelo y trae dos voces en espanol, `ef_dora` (femenina) y
`em_alex` (masculina). Corre sobre el `onnxruntime` que este proyecto ya tiene instalado
para el VAD y el OCR, asi que **no arrastra PyTorch**: 20 paquetes puros de Python, 88 MB de
modelo y medio MB por voz.

**El acento se arregla en el fonemizador, no en la voz.** Kokoro fonemiza con espeak-ng, y
espeak `es` es peninsular: "Centinela, cinco" sale `θentinˈela, θˈinko`. Con `es-419`
--espanol de America-- sale `sentinˈela, sˈinko`. La voz es la misma; lo que cambia es la
pronunciacion, y es la diferencia entre que un paciente colombiano oiga a alguien de aqui o
a alguien de fuera.

Lo que este script NO decide es el timbre. Escribe los WAV de las mismas frases con cada
candidata en `data/ab_voz/` y la eleccion es de quien oye.

    python scripts/ab_voz_kokoro.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))

from centinela.dialog import script as S  # noqa: E402
from centinela.tts.piper import DIR_PIPER, PiperTTS, _muestras  # noqa: E402

DIR_KOKORO = RAIZ / "data" / "kokoro"
DIR_MUESTRAS = RAIZ / "data" / "ab_voz"
DESTINO = RAIZ / "docs" / "metrics" / "ab_voz_kokoro.json"

# El modelo cuantizado y no el `q8f16`: ese revienta al cargar con onnxruntime 1.28 --
# violacion de acceso, sin excepcion de Python que atrapar. Queda escrito porque es el que
# la pagina del modelo lista primero por tamano.
MODELO = "model_quantized.onnx"

FRASES = (
    ("saludo", S.SALUDO.texto),
    ("cierre_rojo", S.CIERRE_ROJO.texto),
    ("pregunta_dolor", S.PREGUNTA_POR_DOMINIO["dolor"].inicial.texto),
    ("silencio_acompanar", S.SILENCIO_ACOMPANAR.texto),
)

# Las candidatas femeninas, mas la masculina de Piper que sirve de referencia porque es la
# que el oido ya aprobo.
CANDIDATAS_PIPER = ("es_AR-daniela-high", "es_ES-davefx-medium")
CANDIDATAS_KOKORO = (("ef_dora", "es-419"), ("ef_dora", "es"))


def _escribir(ruta: Path, muestras: np.ndarray, frecuencia: int) -> None:
    pcm = np.clip(muestras * 32767, -32768, 32767).astype("<i2")
    with wave.open(str(ruta), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(frecuencia)
        w.writeframes(pcm.tobytes())


def _voces_juntas() -> Path:
    """El paquete espera un archivo con todas las voces; se publican sueltas."""

    combinado = DIR_KOKORO / "voices.npz"
    if not combinado.exists():
        sueltas = {}
        for archivo in sorted(DIR_KOKORO.glob("*.bin")):
            sueltas[archivo.stem] = np.fromfile(
                archivo, dtype=np.float32
            ).reshape(-1, 1, 256)
        np.savez(combinado, **sueltas)
    return combinado


FRECUENCIA_KOKORO = 24000


def medir_kokoro(voz: str, idioma: str) -> dict:
    """Alimenta la sesion ONNX directamente, sin pasar por `Kokoro.create`.

    El paquete `kokoro-onnx` se escribio contra otro export del mismo modelo y le manda a
    `speed` un entero donde este espera un flotante: `Unexpected input data type. Actual:
    (tensor(int32)), expected: (tensor(float))`. Depurar el acoplamiento de un paquete
    contra un export distinto es mas trabajo que las veinte lineas de aqui, y ademas quita
    una dependencia del camino critico: del paquete solo se usa el fonemizador.

    La firma del modelo es explicita y no hay que adivinarla:
        input_ids  int64   [1, n]      fonemas, con el 0 de frontera a los dos lados
        style      float32 [1, 256]    el vector de la voz, indexado por la longitud
        speed      float32 [1]
    """

    import onnxruntime as ort
    from kokoro_onnx.tokenizer import Tokenizer

    salida = DIR_MUESTRAS / f"kokoro_{voz}_{idioma.replace('-', '')}"
    salida.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    sesion = ort.InferenceSession(
        str(DIR_KOKORO / MODELO), providers=["CPUExecutionProvider"]
    )
    tk = Tokenizer()
    estilos = np.fromfile(
        DIR_KOKORO / f"{voz}.bin", dtype=np.float32
    ).reshape(-1, 1, 256)
    ms_arranque = (time.perf_counter() - t0) * 1000.0

    audio_total = 0.0
    computo = 0.0
    primera = None
    for clave, texto in FRASES:
        t = time.perf_counter()
        trozos = []
        # Kokoro tiene un tope de 510 fonemas por pasada, asi que las locuciones largas
        # --el cierre rojo son casi 30 s-- se parten por frase y se concatenan.
        for frase in [f for f in texto.replace("\n", " ").split(". ") if f.strip()]:
            ids = tk.tokenize(tk.phonemize(frase.strip() + ".", lang=idioma))[:510]
            if ids:
                estilo = estilos[len(ids)].astype(np.float32)
                onda = sesion.run(None, {
                    "input_ids": np.array([[0, *ids, 0]], dtype=np.int64),
                    "style": estilo,
                    "speed": np.array([1.0], dtype=np.float32),
                })[0]
                trozos.append(np.asarray(onda).reshape(-1))
        muestras = np.concatenate(trozos) if trozos else np.zeros(0, dtype=np.float32)
        ms = (time.perf_counter() - t) * 1000.0
        _escribir(salida / f"{clave}.wav", muestras, FRECUENCIA_KOKORO)
        audio_total += len(muestras) / FRECUENCIA_KOKORO
        computo += ms / 1000.0
        if primera is None:
            primera = ms

    return {
        "motor": "kokoro",
        "voz": f"{voz} ({idioma})",
        "mb_modelo": round((DIR_KOKORO / MODELO).stat().st_size / 1024 / 1024, 1),
        "ms_arranque": round(ms_arranque, 1),
        "rtf_medio": round(computo / audio_total, 3) if audio_total else None,
        "ms_primera_locucion": round(primera or 0.0, 1),
        "muestras_en": str(salida.relative_to(RAIZ)),
    }


async def medir_piper(nombre: str) -> dict:
    ruta = DIR_PIPER / "voces" / f"{nombre}.onnx"
    salida = DIR_MUESTRAS / f"piper_{nombre}"
    salida.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    tts = PiperTTS(voz=ruta, dir_cache=salida)
    audio_total = 0.0
    computo = 0.0
    primera = None
    try:
        for clave, texto in FRASES:
            t = time.perf_counter()
            audio = await tts.sintetizar(texto)
            ms = (time.perf_counter() - t) * 1000.0
            (salida / f"{clave}.wav").write_bytes(audio.wav or b"")
            datos, frecuencia = _muestras(audio.wav)
            audio_total += len(datos) / frecuencia
            computo += ms / 1000.0
            if primera is None:
                primera = ms
    finally:
        await tts.cerrar()

    return {
        "motor": "piper",
        "voz": nombre,
        "mb_modelo": round(ruta.stat().st_size / 1024 / 1024, 1),
        "ms_arranque": round((time.perf_counter() - t0) * 1000.0, 1),
        "rtf_medio": round(computo / audio_total, 3) if audio_total else None,
        "ms_primera_locucion": round(primera or 0.0, 1),
        "muestras_en": str(salida.relative_to(RAIZ)),
    }


async def main() -> int:
    medidas = []
    for nombre in CANDIDATAS_PIPER:
        if (DIR_PIPER / "voces" / f"{nombre}.onnx").exists():
            print(f"  midiendo piper {nombre}...")
            medidas.append(await medir_piper(nombre))

    if (DIR_KOKORO / MODELO).exists():
        for voz, idioma in CANDIDATAS_KOKORO:
            print(f"  midiendo kokoro {voz} en {idioma}...")
            medidas.append(medir_kokoro(voz, idioma))
    else:
        print(f"  falta {DIR_KOKORO / MODELO}; solo se mide Piper")

    informe = {
        "por_que": (
            "el catalogo de Piper en espanol tiene nueve voces y solo una es femenina y "
            "latinoamericana. Kokoro-82M es otra clase de modelo, trae voz femenina en "
            "espanol y corre sobre el onnxruntime que ya esta instalado."
        ),
        "el_acento_es_del_fonemizador": (
            "espeak `es` es peninsular y dice 'θentinela'; `es-419` dice 'sentinela'. La "
            "voz es la misma y lo que cambia es la pronunciacion."
        ),
        "lo_que_no_mide": "el timbre. Para eso estan los WAV.",
        "medidas": medidas,
    }
    DESTINO.parent.mkdir(parents=True, exist_ok=True)
    DESTINO.write_text(
        json.dumps(informe, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print()
    print("=" * 86)
    print("A/B DE VOZ - Piper contra Kokoro")
    print("=" * 86)
    print()
    print(f"  {'motor':8s}{'voz':26s}{'MB':>6s}{'arranque':>10s}{'RTF':>8s}{'1a loc.':>10s}")
    print("  " + "-" * 76)
    for m in medidas:
        print(f"  {m['motor']:8s}{m['voz']:26s}{m['mb_modelo']:>6.1f}"
              f"{m['ms_arranque']:>9.0f}ms{m['rtf_medio'] or 0:>8.3f}"
              f"{m['ms_primera_locucion']:>8.0f}ms")
    print()
    print("  El RTF solo cuenta en el 15 % de turnos que sintetizan en vivo: el resto sale")
    print("  del cache pre-renderizado, donde se paga una vez al arrancar.")
    print()
    for m in medidas:
        print(f"  {m['voz']:26s} -> {m['muestras_en']}")
    print()
    print(f"  informe en {DESTINO.relative_to(RAIZ)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
