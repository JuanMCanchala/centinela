"""Parte una grabacion seguida en los 18 audios que espera `eval/escucha.py`.

Grabar dieciocho ficheros a mano es tedioso, asi que lo comodo es decir las
dieciocho frases del tiron, en orden, con una pausa entre cada una. Este script
corta esa grabacion.

Como decide donde cortar: mide la energia en ventanas de 20 ms y separa por
silencio. La clave es distinguir dos silencios distintos, y en una grabacion real
se distinguen solos por su duracion:

  - la pausa DENTRO de una frase -- la coma de "Si, soy yo" -- dura decimas
  - la pausa ENTRE frases, cuando la persona respira y pasa a la siguiente, dura
    segundos

En la grabacion con la que se escribio esto: pausas internas de 0.46 y 0.66 s,
pausas entre frases de 2.46 s como minimo. `SEGUNDOS_ENTRE_FRASES` se pone entre
las dos, y por eso el corte es fiable sin ajustar nada a mano.

Si el numero de trozos no coincide con el numero de frases del guion, el script se
para y lo dice en vez de escribir dieciocho ficheros mal emparejados -- que seria
mucho peor, porque cada audio se compara contra la frase que le toca por posicion.

Uso:

    python scripts/partir_grabacion.py grabacion.mp3
    python scripts/partir_grabacion.py grabacion.mp3 --umbral-pausa 1.5

Acepta cualquier formato que lea ffmpeg (mp3, m4a, mp4, wav...). Necesita ffmpeg
en el PATH solo si la entrada no es WAV.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from eval.escucha import DIR_AUDIOS, GUION  # noqa: E402

FRECUENCIA = 16000
VENTANA_S = 0.020

# Pausa a partir de la cual se considera que empieza otra frase. Entre la pausa de
# una coma (decimas) y la pausa de pasar a la frase siguiente (segundos).
SEGUNDOS_ENTRE_FRASES = 1.2

# Un trozo con voz mas corto que esto es un carraspeo, no una frase.
MINIMO_FRASE_S = 0.18

# Aire que se deja a cada lado del corte. Whisper transcribe peor cuando el audio
# empieza pegado a la primera silaba, y ademas asi el recorte se parece a lo que
# manda el navegador, que arrastra su propio pre-roll.
AIRE_S = 0.25


def a_wav_16k(entrada: Path, destino: Path) -> None:
    """Convierte cualquier cosa a WAV mono 16 kHz con ffmpeg."""

    if shutil.which("ffmpeg") is None:
        raise SystemExit(
            "Hace falta ffmpeg en el PATH para leer "
            f"{entrada.suffix}. Con WAV no se necesita."
        )
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(entrada),
         "-ac", "1", "-ar", str(FRECUENCIA), "-c:a", "pcm_s16le", str(destino)],
        check=True,
    )


def leer(ruta: Path) -> np.ndarray:
    with wave.open(str(ruta), "rb") as w:
        if w.getframerate() != FRECUENCIA or w.getnchannels() != 1:
            raise ValueError("se esperaba WAV mono a 16 kHz")
        crudo = w.readframes(w.getnframes())
    return np.frombuffer(crudo, dtype="<i2").astype(np.float32) / 32768.0


def escribir(ruta: Path, muestras: np.ndarray) -> None:
    enteros = np.clip(muestras * 32768.0, -32768, 32767).astype("<i2")
    with wave.open(str(ruta), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(FRECUENCIA)
        w.writeframes(enteros.tobytes())


def trozos_con_voz(x: np.ndarray, umbral_pausa: float) -> list[tuple[float, float]]:
    ventana = int(VENTANA_S * FRECUENCIA)
    n = len(x) // ventana
    rms = np.array([
        float(np.sqrt(np.mean(x[i * ventana:(i + 1) * ventana] ** 2))) for i in range(n)
    ])

    # Umbral relativo: el suelo de esta grabacion y el pico de esta grabacion. Un
    # valor absoluto no sirve porque depende del microfono y de la distancia.
    piso = float(np.percentile(rms, 20))
    umbral = max(piso * 4, float(rms.max()) * 0.06)

    activa = rms > umbral

    # Cerrar los silencios cortos: son pausas dentro de una misma frase.
    huecos = int(umbral_pausa / VENTANA_S)
    i = 0
    while i < len(activa):
        if activa[i]:
            i += 1
        else:
            j = i
            while j < len(activa) and not activa[j]:
                j += 1
            interior = i > 0 and j < len(activa)
            if interior and (j - i) < huecos:
                activa[i:j] = True
            i = j

    segmentos: list[tuple[float, float]] = []
    i = 0
    while i < len(activa):
        if activa[i]:
            j = i
            while j < len(activa) and activa[j]:
                j += 1
            if (j - i) * VENTANA_S >= MINIMO_FRASE_S:
                segmentos.append((i * VENTANA_S, j * VENTANA_S))
            i = j
        else:
            i += 1

    return segmentos


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("grabacion")
    ap.add_argument("--umbral-pausa", type=float, default=SEGUNDOS_ENTRE_FRASES,
                    help="segundos de silencio que separan una frase de la siguiente")
    ap.add_argument("--destino", default=str(DIR_AUDIOS))
    ap.add_argument("--forzar", action="store_true",
                    help="escribe aunque el numero de trozos no cuadre con el guion")
    args = ap.parse_args()

    entrada = Path(args.grabacion)
    if not entrada.exists():
        print(f"No existe {entrada}")
        return 2

    temporal = None
    if entrada.suffix.lower() == ".wav":
        fuente = entrada
    else:
        temporal = entrada.with_suffix(".16k.wav")
        a_wav_16k(entrada, temporal)
        fuente = temporal

    x = leer(fuente)
    print(f"{entrada.name}: {len(x) / FRECUENCIA:.2f}s, pico {np.abs(x).max():.3f}")

    segmentos = trozos_con_voz(x, args.umbral_pausa)
    print(f"pausa que separa frases: {args.umbral_pausa}s")
    print(f"trozos con voz detectados: {len(segmentos)}  (el guion tiene {len(GUION)})")
    print()

    codigo = 0
    if len(segmentos) != len(GUION) and not args.forzar:
        for k, (a, b) in enumerate(segmentos, 1):
            print(f"  {k:2d}. {a:6.2f}s -> {b:6.2f}s  ({b - a:.2f}s)")
        print()
        print("No cuadra con el guion, asi que no se escribe nada: cada audio se")
        print("compara contra la frase que le toca POR POSICION, y emparejarlos mal")
        print("daria un informe entero de fallos falsos.")
        print()
        print("Si hay mas trozos que frases, alguna frase se partio por dentro:")
        print("  suba --umbral-pausa (por ejemplo a 1.5)")
        print("Si hay menos, dos frases quedaron pegadas:")
        print("  baje --umbral-pausa (por ejemplo a 0.9)")
        codigo = 1
    else:
        destino = Path(args.destino)
        destino.mkdir(parents=True, exist_ok=True)
        aire = int(AIRE_S * FRECUENCIA)

        for ficha, (a, b) in zip(GUION, segmentos):
            desde = max(0, int(a * FRECUENCIA) - aire)
            hasta = min(len(x), int(b * FRECUENCIA) + aire)
            trozo = x[desde:hasta]
            escribir(destino / ficha.archivo, trozo)
            print(f"  {ficha.archivo:32s} {len(trozo) / FRECUENCIA:5.2f}s   \"{ficha.dice}\"")

        print()
        print(f"{len(segmentos)} audios escritos en {destino}")
        print("Ahora: make escucha")

    if temporal is not None and temporal.exists():
        temporal.unlink()

    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
