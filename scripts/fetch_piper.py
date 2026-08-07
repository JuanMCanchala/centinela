"""Descarga el binario de Piper y una voz en espanol.

Piper se usa como binario externo, no como wheel de Python: `piper-tts` depende
de `piper-phonemize`, que no publica wheel para `win_amd64` (solo manylinux y
macos). El binario nativo existe para las tres plataformas y expone el mismo
contrato -- entra texto por stdin, sale WAV por stdout -- asi que el codigo de
`tts/piper.py` es identico en Windows y en el contenedor Linux.

    python scripts/fetch_piper.py
"""

from __future__ import annotations

import io
import time
import platform
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
DESTINO = RAIZ / "data" / "piper"

VERSION = "2023.11.14-2"
BASE = f"https://github.com/rhasspy/piper/releases/download/{VERSION}"

PAQUETES = {
    ("Windows", "AMD64"): ("piper_windows_amd64.zip", "zip"),
    ("Linux", "x86_64"): ("piper_linux_x86_64.tar.gz", "tar"),
    ("Linux", "aarch64"): ("piper_linux_aarch64.tar.gz", "tar"),
    ("Darwin", "x86_64"): ("piper_macos_x64.tar.gz", "tar"),
    ("Darwin", "arm64"): ("piper_macos_aarch64.tar.gz", "tar"),
}

# Voces candidatas, en orden de preferencia.
#
# El criterio de orden es LATENCIA, no timbre. Medimos las tres calidades de
# Piper en la misma maquina (`scripts/bench_voz.py`): la variante `high` corre a
# un factor de tiempo real de ~1.0 en CPU, es decir tarda en generar un segundo
# de audio lo mismo que dura ese segundo. Eso es inservible para una
# conversacion. Las `medium` y `x_low` estan un orden de magnitud por debajo.
#
# Dentro de cada calidad se prefiere el acento latinoamericano (es_MX, es_AR)
# sobre el peninsular: el paciente del reto es colombiano.
_HF = "https://huggingface.co/rhasspy/piper-voices/resolve/main/es"

VOCES = (
    ("es_MX-ald-medium", f"{_HF}/es_MX/ald/medium/es_MX-ald-medium.onnx"),
    ("es_ES-sharvard-medium", f"{_HF}/es_ES/sharvard/medium/es_ES-sharvard-medium.onnx"),
    ("es_ES-davefx-medium", f"{_HF}/es_ES/davefx/medium/es_ES-davefx-medium.onnx"),
    ("es_ES-carlfm-x_low", f"{_HF}/es_ES/carlfm/x_low/es_ES-carlfm-x_low.onnx"),
    ("es_MX-claude-high", f"{_HF}/es_MX/claude/high/es_MX-claude-high.onnx"),
    ("es_AR-daniela-high", f"{_HF}/es_AR/daniela/high/es_AR-daniela-high.onnx"),
)


def descargar(url: str, intentos: int = 4) -> bytes:
    """Descarga con reintentos.

    Los reintentos no son paranoia: durante el desarrollo la resolucion DNS de
    huggingface.co y de registry.ollama.ai fallo de forma intermitente
    (`getaddrinfo failed`), y sin reintentos eso se confunde con un 404 y lleva
    a descartar un recurso que si existe.
    """

    ultimo: Exception | None = None
    datos = b""

    for intento in range(intentos):
        try:
            peticion = urllib.request.Request(url, headers={"User-Agent": "centinela/0.1"})
            with urllib.request.urlopen(peticion, timeout=300) as r:
                datos = r.read()
            ultimo = None
            break
        except Exception as e:  # noqa: BLE001
            ultimo = e
            espera = 2 ** intento
            print(f"    intento {intento + 1}/{intentos} fallo ({type(e).__name__}), "
                  f"reintento en {espera}s")
            time.sleep(espera)

    if ultimo is not None:
        raise ultimo
    return datos


def bajar_binario() -> Path | None:
    clave = (platform.system(), platform.machine())
    if clave not in PAQUETES:
        print(f"plataforma no soportada por los binarios de Piper: {clave}")
        return None

    nombre, tipo = PAQUETES[clave]
    url = f"{BASE}/{nombre}"
    print(f"bajando {url}")
    try:
        datos = descargar(url)
    except Exception as e:
        print(f"  fallo: {type(e).__name__}: {e}")
        return None

    DESTINO.mkdir(parents=True, exist_ok=True)
    if tipo == "zip":
        with zipfile.ZipFile(io.BytesIO(datos)) as z:
            z.extractall(DESTINO)
    else:
        with tarfile.open(fileobj=io.BytesIO(datos), mode="r:gz") as t:
            t.extractall(DESTINO)

    sufijo = ".exe" if platform.system() == "Windows" else ""
    candidatos = list(DESTINO.rglob(f"piper{sufijo}"))
    ejecutables = [c for c in candidatos if c.is_file()]
    if not ejecutables:
        print("  el paquete no contenia el ejecutable piper")
        return None

    binario = ejecutables[0]
    if platform.system() != "Windows":
        binario.chmod(0o755)
    print(f"  binario en {binario}")
    return binario


def bajar_voz(todas: bool = False) -> tuple[str, Path] | None:
    """Baja la primera voz disponible, o todas si `todas` es True.

    Bajar todas sirve para comparar latencia y timbre entre calidades y elegir
    con datos (`scripts/bench_voces.py`).
    """

    voces_dir = DESTINO / "voces"
    voces_dir.mkdir(parents=True, exist_ok=True)
    primera: tuple[str, Path] | None = None

    for nombre, url_onnx in VOCES:
        destino_onnx = voces_dir / f"{nombre}.onnx"
        destino_json = voces_dir / f"{nombre}.onnx.json"

        if destino_onnx.exists() and destino_json.exists():
            print(f"voz ya presente: {nombre}")
            if primera is None:
                primera = (nombre, destino_onnx)
            if not todas:
                break
        else:
            print(f"bajando voz {nombre}")
            try:
                datos = descargar(url_onnx)
                config = descargar(url_onnx + ".json")
            except Exception as e:  # noqa: BLE001
                print(f"  no disponible ({type(e).__name__}: {e}), pruebo la siguiente")
                continue

            destino_onnx.write_bytes(datos)
            destino_json.write_bytes(config)
            print(f"  voz en {destino_onnx.name}  ({len(datos)/1024/1024:.1f} MB)")
            if primera is None:
                primera = (nombre, destino_onnx)
            if not todas:
                break

    if primera is None:
        print("ninguna voz en espanol se pudo descargar")
    return primera


def main() -> int:
    binario = bajar_binario()
    voz = bajar_voz(todas="--todas" in sys.argv)

    if binario is None or voz is None:
        print()
        print("Piper no quedo disponible. El sistema usara el TTS del navegador")
        print("como respaldo (ver docs/arquitectura.md, seccion Voz).")
        codigo = 1
    else:
        print()
        print(f"Piper listo: {binario}")
        print(f"Voz        : {voz[0]}")
        codigo = 0
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
