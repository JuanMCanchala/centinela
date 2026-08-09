"""Compara configuraciones de transcripcion sobre frases reales de paciente.

Existe por un fallo concreto y visible: el paciente dijo *"si, soy yo"* y el
sistema transcribio *"Season young"* -- palabras inglesas, con `language="es"`
forzado. La misma frase daba *"Ce soy yo"* en otra corrida.

El patron es claro: las frases MUY cortas son el punto debil de Whisper, y la
configuracion que elegi al principio empeoraba justo ese caso. Este script mide
cual funciona en vez de discutirlo.

Metrica: tasa de error por palabra (WER) contra la transcripcion conocida, y el
tiempo. Se pesa aparte el grupo de frases cortas, que es donde duele.

**El audio son ficheros, y esto es la correccion de un fallo del propio arnes.** Antes
cada corrida sintetizaba las frases con Piper. Piper no es determinista, asi que el
material de prueba cambiaba solo: `small cpu beam5` puntuo WER 0.271 en frases cortas
una vez y **9.646** la siguiente -- no es ruido, es una alucinacion desbocada sobre un
audio distinto. Con eso la linea "Mejor en frases cortas" señalaba una configuracion
diferente cada vez, y el informe se publicaba igual. Un arnes que elige al ganador por
sorteo es peor que no tenerlo, porque parece que decide.

Ahora el audio son las 18 grabaciones humanas de `eval/audios`, con el texto de
referencia que ya declara `eval/escucha.py` en su `GUION`. Son ficheros: la misma
configuracion da el mismo numero. Y la referencia vive en un solo sitio, asi que
`bench_stt` (que compara configuraciones) y `escucha` (que evalua la de produccion) no
pueden discrepar sobre lo que el paciente dijo.

Efecto secundario, y no menor: sobre voz humana el orden cambia. `large-v3-turbo` en
GPU alucinaba en las frases cortas de Piper y es la mejor sobre voz real.

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

# Las frases y su texto de referencia salen del `GUION` de `eval/escucha.py`, que es
# donde vive la verdad sobre lo que dice cada grabacion. Aqui habia una copia --
# `CORTAS` y `LARGAS`, sintetizadas con Piper -- y esa copia es la que hacia el arnes
# irreproducible.

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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--con-medium", action="store_true",
                    help="incluye whisper medium (1.5 GB de descarga)")
    args = ap.parse_args()

    from faster_whisper import WhisperModel

    configs = dict(CONFIGS)

    # `--con-medium` se parseaba y no se usaba: pasarlo no anadia ninguna fila y la
    # comparativa salia igual, sin decir que la opcion no habia hecho nada. `medium` es
    # justo el primer peldano de la escalera de `stt/whisper.py`, asi que era la unica
    # configuracion en produccion que este arnes no sabia medir.
    if args.con_medium:
        configs["medium gpu beam5 + prompt"] = {
            "tamano": "medium", "dispositivo": "cuda", "computo": "int8_float16",
            "beam_size": 5, "prompt": PROMPT_CLINICO, "temperature": FALLBACK,
            "vad": True,
        }
        configs["medium cpu beam5 + prompt"] = {
            "tamano": "medium", "dispositivo": "cpu", "computo": "int8",
            "beam_size": 5, "prompt": PROMPT_CLINICO, "temperature": FALLBACK,
            "vad": True,
        }

    # Grabaciones humanas, no sintesis. Ver la nota de cabecera: sintetizar el audio
    # en cada corrida hacia que la misma configuracion puntuara 0.271 una vez y 9.646
    # la siguiente, y con eso la comparativa elegia una ganadora distinta cada vez.
    from eval.escucha import DIR_AUDIOS, GUION, leer_wav

    print(f"leyendo grabaciones humanas de {DIR_AUDIOS.relative_to(RAIZ)}...")
    frases: list[tuple[str, str]] = []
    audios: dict[str, np.ndarray] = {}
    faltan: list[str] = []
    for ficha in GUION:
        ruta = DIR_AUDIOS / ficha.archivo
        if ruta.is_file():
            # "corta" son los turnos de una a tres palabras, que es donde Whisper es
            # mas debil y donde el paciente responde mas breve.
            grupo = "corta" if len(ficha.dice.split()) <= 3 else "larga"
            frases.append((ficha.dice, grupo))
            audios[ficha.dice] = leer_wav(ruta)
        else:
            faltan.append(ficha.archivo)

    if not audios:
        print(f"no hay grabaciones en {DIR_AUDIOS}: corra `python -m eval.escucha --guion`")
        return 2
    if faltan:
        print(f"  AVISO: faltan {len(faltan)} grabaciones del guion: {', '.join(faltan)}")
    n_cortas = sum(1 for _, g in frases if g == "corta")
    print(f"{len(audios)} grabaciones listas ({n_cortas} cortas, "
          f"{len(frases) - n_cortas} largas)\n")

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
