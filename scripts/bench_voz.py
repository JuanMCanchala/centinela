"""Benchmark del camino de voz: STT y TTS por separado.

Produce los numeros que el README debe reportar desglosados por etapa. Reportar
solo el total es facil de discutir; el desglose es lo que hace creible la cifra.

    python scripts/bench_voz.py
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))

from centinela.dialog.script import todas_las_locuciones  # noqa: E402
from centinela.tts.piper import PiperTTS, partir_en_frases  # noqa: E402

FRASES_PRUEBA = (
    "Entendido, gracias.",
    "Ha tenido fiebre o se ha sentido caliente o con escalofrios?",
    "Voy a detener las preguntas aqui, porque lo que me acaba de contar es importante.",
    "Lo que me describe es un signo de alarma despues de una apendicectomia. "
    "Ya deje una alerta al equipo clinico con sus datos.",
)


async def bench_tts() -> dict:
    tts = PiperTTS()
    print("=" * 78)
    print("TTS - Piper")
    print("=" * 78)
    estado = tts.estado()
    for k, v in estado.items():
        print(f"  {k:24s}: {v}")

    if not tts.disponible:
        print("\n  Piper no disponible; se omite el benchmark de TTS")
        return {"disponible": False}

    print("\n  pre-renderizando el guion completo...")
    pre = await tts.pre_renderizar(todas_las_locuciones())
    print(f"    generadas={pre['generadas']} reutilizadas={pre['reutilizadas']} "
          f"fallidas={len(pre['fallidas'])} en {pre['segundos']}s")
    if pre["fallidas"]:
        print(f"    FALLIDAS: {pre['fallidas']}")

    print("\n  sintesis en caliente (texto no cacheado):")
    resultados = {}
    for frase in FRASES_PRUEBA:
        tiempos = []
        for _ in range(3):
            audio = await tts.sintetizar(frase)
            tiempos.append(audio.ms_sintesis)
        mediana = statistics.median(tiempos)
        audio = await tts.sintetizar(frase)
        rtf = (mediana / 1000) / max(0.01, audio.duracion_s)
        resultados[frase[:40]] = {
            "ms_p50": round(mediana, 1),
            "duracion_audio_s": round(audio.duracion_s, 2),
            "factor_tiempo_real": round(rtf, 3),
            "caracteres": len(frase),
        }
        print(f"    {len(frase):3d} car  {mediana:7.1f} ms  "
              f"audio {audio.duracion_s:4.1f}s  RTF {rtf:.3f}  \"{frase[:44]}\"")

    print("\n  lectura desde cache (turno de guion):")
    claves = [l.clave for l in todas_las_locuciones() if "{" not in l.texto][:8]
    tiempos_cache = []
    for clave in claves:
        t0 = time.perf_counter()
        audio = await tts.sintetizar("", clave=clave)
        tiempos_cache.append((time.perf_counter() - t0) * 1000)
    p50_cache = statistics.median(tiempos_cache)
    print(f"    {len(claves)} locuciones, p50 = {p50_cache:.3f} ms   "
          f"max = {max(tiempos_cache):.3f} ms")

    print("\n  troceo en frases (streaming):")
    largo = FRASES_PRUEBA[-1]
    frases = partir_en_frases(largo)
    t0 = time.perf_counter()
    primera = await tts.sintetizar(frases[0])
    ms_primera = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    completo = await tts.sintetizar(largo)
    ms_completo = (time.perf_counter() - t0) * 1000
    print(f"    respuesta completa : {ms_completo:7.1f} ms hasta el primer audio")
    print(f"    primera frase sola : {ms_primera:7.1f} ms hasta el primer audio")
    print(f"    ganancia           : {ms_completo - ms_primera:7.1f} ms "
          f"({100 * (1 - ms_primera / ms_completo):.0f}% menos)")

    return {
        "disponible": True,
        "estado": estado,
        "pre_renderizado": pre,
        "sintesis_en_caliente": resultados,
        "cache_ms_p50": round(p50_cache, 4),
        "cache_ms_max": round(max(tiempos_cache), 4),
        "streaming": {
            "ms_respuesta_completa": round(ms_completo, 1),
            "ms_primera_frase": round(ms_primera, 1),
            "n_frases": len(frases),
        },
    }


def bench_stt() -> dict:
    print()
    print("=" * 78)
    print("STT - faster-whisper")
    print("=" * 78)
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        print(f"  faster-whisper no disponible: {e}")
        return {"disponible": False}

    import numpy as np

    tamano = "small"
    print(f"  cargando modelo '{tamano}' (int8, CPU)...")
    t0 = time.perf_counter()
    modelo = WhisperModel(tamano, device="cpu", compute_type="int8",
                          download_root=str(RAIZ / "data" / "modelos" / "whisper"))
    ms_carga = (time.perf_counter() - t0) * 1000
    print(f"    cargado en {ms_carga/1000:.1f}s")

    # Audio sintetico: no mide exactitud, mide latencia de proceso por segundo
    # de audio, que es lo que entra en el presupuesto de la conversacion.
    resultados = {}
    for segundos in (2, 4, 8):
        muestras = np.zeros(16000 * segundos, dtype=np.float32)
        muestras += np.random.default_rng(0).normal(0, 0.001, muestras.shape).astype(np.float32)
        tiempos = []
        for _ in range(3):
            t0 = time.perf_counter()
            segmentos, _info = modelo.transcribe(
                muestras, language="es", vad_filter=True, beam_size=1
            )
            list(segmentos)
            tiempos.append((time.perf_counter() - t0) * 1000)
        p50 = statistics.median(tiempos)
        resultados[f"{segundos}s"] = {
            "ms_p50": round(p50, 1),
            "factor_tiempo_real": round((p50 / 1000) / segundos, 3),
        }
        print(f"    audio de {segundos}s -> {p50:7.1f} ms   "
              f"RTF {(p50/1000)/segundos:.3f}")

    return {
        "disponible": True,
        "modelo": tamano,
        "dispositivo": "cpu/int8",
        "ms_carga": round(ms_carga, 1),
        "por_duracion": resultados,
    }


async def main() -> int:
    informe = {"tts": await bench_tts(), "stt": bench_stt()}
    destino = RAIZ / "docs" / "metrics" / "bench_voz.json"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(informe, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\ninforme escrito en {destino.relative_to(RAIZ)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
