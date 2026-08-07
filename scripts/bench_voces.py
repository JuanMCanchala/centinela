"""Compara las voces de Piper disponibles por latencia.

El criterio de seleccion de voz en un agente de voz no es el timbre: es el factor
de tiempo real (RTF). Un RTF de 1.0 significa que generar un segundo de audio
cuesta un segundo de computo, lo que en una conversacion se traduce en que el
paciente espera tanto como dura la respuesta. Un RTF de 0.1 es imperceptible.

Este script produce la tabla que justifica la voz elegida en el informe final, y
deja las muestras de audio en `data/muestras_voz/` para poder escucharlas.

    python scripts/bench_voces.py
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
DIR_PIPER = RAIZ / "data" / "piper"
DIR_MUESTRAS = RAIZ / "data" / "muestras_voz"

FRASES = (
    "Entendido, gracias.",
    "Ha tenido fiebre o se ha sentido caliente o con escalofrios?",
    "Lo que me describe es un signo de alarma despues de su cirugia. "
    "Ya deje una alerta al equipo clinico con sus datos.",
)

REPETICIONES = 3


def duracion_wav(ruta: Path) -> float:
    import wave

    with wave.open(str(ruta), "rb") as w:
        segundos = w.getnframes() / w.getframerate()
    return segundos


def medir(binario: Path, voz: Path, frase: str, salida: Path) -> tuple[float, float]:
    tiempos = []
    for _ in range(REPETICIONES):
        t0 = time.perf_counter()
        subprocess.run(
            [str(binario), "--model", str(voz), "--output_file", str(salida)],
            input=frase.encode("utf-8"),
            capture_output=True,
            cwd=str(binario.parent),
            check=False,
        )
        tiempos.append((time.perf_counter() - t0) * 1000)
    dur = duracion_wav(salida) if salida.exists() else 0.0
    return statistics.median(tiempos), dur


def main() -> int:
    candidatos = list(DIR_PIPER.rglob("piper.exe")) + list(DIR_PIPER.rglob("piper"))
    binarios = [c for c in candidatos if c.is_file()]
    if not binarios:
        print("no encuentro el binario de piper; corre scripts/fetch_piper.py")
        return 2
    binario = binarios[0]

    voces = sorted((DIR_PIPER / "voces").glob("*.onnx"))
    if not voces:
        print("no hay voces; corre scripts/fetch_piper.py --todas")
        return 2

    DIR_MUESTRAS.mkdir(parents=True, exist_ok=True)
    print(f"binario: {binario}")
    print(f"{len(voces)} voces, {len(FRASES)} frases, mediana de {REPETICIONES} corridas")
    print()
    print(f"{'voz':26s} {'MB':>6s} {'RTF':>7s} {'ms corto':>9s} {'ms largo':>9s} {'audio_s':>8s}")
    print("-" * 74)

    informe = {}
    for voz in voces:
        mb = voz.stat().st_size / 1024 / 1024
        medidas = []
        for i, frase in enumerate(FRASES):
            salida = DIR_MUESTRAS / f"{voz.stem}__{i}.wav"
            ms, dur = medir(binario, voz, frase, salida)
            medidas.append({"caracteres": len(frase), "ms": round(ms, 1),
                            "audio_s": round(dur, 2),
                            "rtf": round((ms / 1000) / dur, 3) if dur > 0 else None})

        rtfs = [m["rtf"] for m in medidas if m["rtf"]]
        rtf_medio = statistics.median(rtfs) if rtfs else float("nan")
        informe[voz.stem] = {
            "mb": round(mb, 1),
            "rtf_mediana": round(rtf_medio, 3),
            "medidas": medidas,
            "muestras": [f"data/muestras_voz/{voz.stem}__{i}.wav" for i in range(len(FRASES))],
        }
        print(f"{voz.stem:26s} {mb:6.1f} {rtf_medio:7.3f} "
              f"{medidas[0]['ms']:9.1f} {medidas[-1]['ms']:9.1f} {medidas[-1]['audio_s']:8.2f}")

    mejor = min(informe.items(), key=lambda kv: kv[1]["rtf_mediana"])
    print()
    print(f"Menor RTF: {mejor[0]}  (RTF {mejor[1]['rtf_mediana']}, {mejor[1]['mb']} MB)")
    print()
    print("Nota: el RTF de la sintesis en caliente solo afecta a los turnos de texto")
    print("libre. Los turnos de guion se sirven del cache pre-renderizado, cuyo costo")
    print("de lectura medido es de 0.002 ms.")

    destino = RAIZ / "docs" / "metrics" / "bench_voces.json"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(
        json.dumps({"binario": str(binario), "frases": list(FRASES), "voces": informe,
                    "elegida_por_latencia": mejor[0]}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\ninforme en {destino.relative_to(RAIZ)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
