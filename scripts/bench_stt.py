"""Compara configuraciones de transcripcion sobre frases reales de paciente.

Existe por un fallo concreto y visible: el paciente dijo *"si, soy yo"* y el
sistema transcribio *"Season young"* -- palabras inglesas, con `language="es"`
forzado. La misma frase daba *"Ce soy yo"* en otra corrida.

El patron es claro: las frases MUY cortas son el punto debil de Whisper, y la
configuracion que elegi al principio empeoraba justo ese caso. Este script mide
cual funciona en vez de discutirlo.

Metrica: tasa de error por palabra (WER) contra la transcripcion conocida, y el
tiempo. Se pesa aparte el grupo de frases cortas, que es donde duele.

    python scripts/bench_stt.py [--con-medium]
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import unicodedata
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))
sys.path.insert(0, str(RAIZ))

import numpy as np  # noqa: E402

# Frases que un paciente colombiano diria de verdad, con el texto esperado.
# El primer grupo es el critico: turnos de una o dos palabras.
CORTAS = (
    "Si, soy yo",
    "Si señora",
    "Claro que si",
    "No",
    "Un seis",
    "Como un cuatro",
    "Normal",
    "No he tenido",
)

LARGAS = (
    "El dolor esta como en un seis y no se si es normal a estos dias",
    "Me tome la temperatura y estaba en treinta y siete cinco",
    "La herida la he visto con un liquido amarillo saliendo y huele feo",
    "Camino normal, sin ningun problema, gracias a Dios",
    "Casi no me da hambre, como muy poquito y a veces ni eso",
    "Duermo muy mal, me despierto varias veces en la noche",
    "Uy no parcero, ese dolorcito esta como en un seis, harto molesta la cosa",
)

# Contexto que se le da a Whisper para sesgar el vocabulario. Es la palanca mas
# potente contra el salto al ingles: fija el idioma y el dominio de golpe.
PROMPT_CLINICO = (
    "Llamada de seguimiento postoperatorio en Colombia. El paciente responde sobre "
    "dolor, fiebre, temperatura, movilidad, la herida quirurgica, apetito y sueno. "
    "Ejemplos: si señora, no he tenido fiebre, el dolor esta como en un seis, "
    "la herida se ve enrojecida, tengo treinta y siete cinco de temperatura."
)

FALLBACK = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)

CONFIGS = {
    "small cpu greedy (config original)": {
        "tamano": "small", "dispositivo": "cpu", "computo": "int8",
        "beam_size": 1, "prompt": None, "temperature": 0.0, "vad": True,
    },
    "small cpu beam5": {
        "tamano": "small", "dispositivo": "cpu", "computo": "int8",
        "beam_size": 5, "prompt": None, "temperature": 0.0, "vad": True,
    },
    "small cpu beam5 + prompt": {
        "tamano": "small", "dispositivo": "cpu", "computo": "int8",
        "beam_size": 5, "prompt": PROMPT_CLINICO, "temperature": FALLBACK, "vad": True,
    },
    # A partir de aqui, GPU. La maquina tiene una RTX 4060 y ctranslate2 reporta
    # soporte de float16, asi que el modelo puede crecer sin pagar latencia --
    # justo al contrario: en GPU un modelo grande es mas rapido que `small` en CPU.
    "large-v3-turbo gpu beam5 + prompt": {
        "tamano": "large-v3-turbo", "dispositivo": "cuda", "computo": "float16",
        "beam_size": 5, "prompt": PROMPT_CLINICO, "temperature": FALLBACK, "vad": True,
    },
    "large-v3-turbo gpu beam5 sin prompt": {
        "tamano": "large-v3-turbo", "dispositivo": "cuda", "computo": "float16",
        "beam_size": 5, "prompt": None, "temperature": FALLBACK, "vad": True,
    },
    "large-v3-turbo gpu greedy + prompt": {
        "tamano": "large-v3-turbo", "dispositivo": "cuda", "computo": "float16",
        "beam_size": 1, "prompt": PROMPT_CLINICO, "temperature": FALLBACK, "vad": True,
    },
}


def normalizar(t: str) -> list[str]:
    sin = "".join(
        c for c in unicodedata.normalize("NFD", t.lower())
        if unicodedata.category(c) != "Mn"
    )
    sin = sin.replace("6", "seis").replace("4", "cuatro").replace("37", "treinta y siete")
    return [p for p in re.findall(r"[a-z0-9]+", sin) if p]


def wer(referencia: str, hipotesis: str) -> float:
    """Tasa de error por palabra, por distancia de edicion."""

    r = normalizar(referencia)
    h = normalizar(hipotesis)
    if not r:
        return 0.0 if not h else 1.0

    d = [[0] * (len(h) + 1) for _ in range(len(r) + 1)]
    for i in range(len(r) + 1):
        d[i][0] = i
    for j in range(len(h) + 1):
        d[0][j] = j
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            coste = 0 if r[i - 1] == h[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + coste)
    return d[len(r)][len(h)] / len(r)


def audio_de(texto: str) -> np.ndarray | None:
    """Sintetiza con Piper y devuelve float32 a 16 kHz, como llega del navegador."""

    import asyncio
    import io
    import wave

    from centinela.tts.piper import PiperTTS

    tts = PiperTTS()
    if not tts.disponible:
        return None

    audio = asyncio.run(tts.sintetizar(texto))
    if not audio.wav:
        return None

    with wave.open(io.BytesIO(audio.wav), "rb") as w:
        origen = w.getframerate()
        marcos = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")

    muestras = marcos.astype(np.float32) / 32768.0

    if origen != 16000:
        razon = origen / 16000
        n = int(len(muestras) / razon)
        salida = np.empty(n, dtype=np.float32)
        for i in range(n):
            salida[i] = muestras[int(i * razon):max(int(i * razon) + 1, int((i + 1) * razon))].mean()
        muestras = salida

    return muestras


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--con-medium", action="store_true",
                    help="incluye whisper medium (1.5 GB de descarga)")
    args = ap.parse_args()

    from faster_whisper import WhisperModel

    configs = dict(CONFIGS)

    print("sintetizando audio de prueba con Piper...")
    frases = [(t, "corta") for t in CORTAS] + [(t, "larga") for t in LARGAS]
    audios: dict[str, np.ndarray] = {}
    for texto, _ in frases:
        a = audio_de(texto)
        if a is None:
            print("Piper no disponible")
            return 2
        audios[texto] = a
    print(f"{len(audios)} frases listas\n")

    modelos: dict[str, WhisperModel] = {}
    informe: dict = {"configuraciones": {}}

    for nombre, cfg in configs.items():
        clave = f'{cfg["tamano"]}|{cfg["dispositivo"]}|{cfg["computo"]}'
        if clave not in modelos:
            print(f"cargando whisper {clave} ...")
            try:
                modelos[clave] = WhisperModel(
                    cfg["tamano"],
                    device=cfg["dispositivo"],
                    compute_type=cfg["computo"],
                    download_root=str(RAIZ / "data" / "modelos" / "whisper"),
                )
            except Exception as e:  # noqa: BLE001
                print(f"  no disponible: {type(e).__name__}: {e}")
                print("  se omite esta configuracion\n")
                continue
        modelo = modelos[clave]

        print("=" * 92)
        print(nombre)
        print("=" * 92)

        resultados = []
        for texto, grupo in frases:
            t0 = time.perf_counter()
            segmentos, _info = modelo.transcribe(
                audios[texto],
                language="es",
                beam_size=cfg["beam_size"],
                initial_prompt=cfg["prompt"],
                temperature=cfg["temperature"],
                vad_filter=cfg["vad"],
                condition_on_previous_text=False,
            )
            oido = " ".join(s.text.strip() for s in segmentos).strip()
            ms = (time.perf_counter() - t0) * 1000
            e = wer(texto, oido)
            resultados.append({"texto": texto, "grupo": grupo, "oido": oido,
                               "wer": round(e, 3), "ms": round(ms, 1)})
            marca = "   " if e == 0 else ("~~ " if e < 0.5 else "XX ")
            print(f"  {marca}[{grupo}] wer={e:.2f} {ms:6.0f}ms  {texto!r}")
            if e > 0:
                print(f"           -> {oido!r}")

        cortas = [r for r in resultados if r["grupo"] == "corta"]
        largas = [r for r in resultados if r["grupo"] == "larga"]
        resumen = {
            "wer_cortas": round(statistics.mean(r["wer"] for r in cortas), 4),
            "wer_largas": round(statistics.mean(r["wer"] for r in largas), 4),
            "wer_global": round(statistics.mean(r["wer"] for r in resultados), 4),
            "perfectas": sum(1 for r in resultados if r["wer"] == 0),
            "total": len(resultados),
            "ms_mediana": round(statistics.median(r["ms"] for r in resultados), 1),
            "detalle": resultados,
        }
        informe["configuraciones"][nombre] = resumen
        print(f"\n  WER cortas={resumen['wer_cortas']:.3f}  largas={resumen['wer_largas']:.3f}  "
              f"global={resumen['wer_global']:.3f}  perfectas={resumen['perfectas']}/"
              f"{resumen['total']}  mediana={resumen['ms_mediana']:.0f}ms\n")

    print("=" * 92)
    print("COMPARATIVA")
    print("=" * 92)
    print(f"{'configuracion':46s} {'cortas':>8s} {'largas':>8s} {'global':>8s} "
          f"{'perfectas':>10s} {'ms':>7s}")
    orden = sorted(informe["configuraciones"].items(),
                   key=lambda kv: (kv[1]["wer_cortas"], kv[1]["wer_global"]))
    for nombre, r in orden:
        print(f"{nombre:46s} {r['wer_cortas']:8.3f} {r['wer_largas']:8.3f} "
              f"{r['wer_global']:8.3f} {r['perfectas']:6d}/{r['total']:<3d} "
              f"{r['ms_mediana']:7.0f}")

    mejor = orden[0]
    informe["mejor"] = mejor[0]
    print(f"\nMejor en frases cortas: {mejor[0]}")

    destino = RAIZ / "docs" / "metrics" / "bench_stt.json"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(informe, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"informe en {destino.relative_to(RAIZ)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
