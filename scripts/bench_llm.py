"""Benchmark de los dos modelos permitidos que siguen vivos.

Mide lo que la rubrica pide medir: tiempo hasta el primer token, porque en una
conversacion de voz eso es lo que el paciente percibe como silencio incomodo.
El throughput total importa menos: una respuesta clinica bien disenada son dos
frases, no dos parrafos.

Sin dependencias externas a proposito: usa solo la libreria estandar, para poder
correrlo antes de que el entorno este instalado.

    python scripts/bench_llm.py
"""

from __future__ import annotations

import json
import statistics
import time
import urllib.request
from pathlib import Path

OLLAMA = "http://127.0.0.1:11434"

MODELOS = ("phi3.5:3.8b-mini-instruct-q4_K_M", "llama3.2:1b")

# Prompts representativos de los tres usos reales del modelo en Centinela.
CASOS = {
    "router": (
        "Clasifica el turno del paciente en una de estas categorias y responde SOLO la palabra: "
        "RESPUESTA (contesta lo que se le pregunto), PREGUNTA (pregunta algo clinico), "
        "FUERA (habla de algo ajeno).\n\n"
        "Pregunta del agente: como ha estado el dolor de 0 a 10?\n"
        "Paciente: pues la verdad diria que un 2, aunque a veces se me hace mas\n"
        "Categoria:"
    ),
    "extraccion": (
        "Extrae los datos clinicos del turno del paciente en JSON. Campos: dolor_nrs (0-10 o null), "
        "fiebre_c (numero o null), herida (normal|eritema_leve|secrecion_purulenta|null).\n\n"
        "Paciente: Ay si, eso si me tiene preocupada... la he visto como con un liquido, "
        "amarillo creo, saliendo de ahi. El dolor como un 6.\n\n"
        "JSON:"
    ),
    "respuesta_clinica": (
        "Eres un asistente de seguimiento postoperatorio. Responde en maximo 2 frases cortas, "
        "en espanol colombiano, tono calmado y profesional. Usa SOLO la informacion del contexto.\n\n"
        "Contexto: La presencia de secrecion purulenta en la herida quirurgica es un signo de "
        "infeccion del sitio operatorio y requiere valoracion medica inmediata.\n\n"
        "Paciente pregunta: eso es grave doctora?\n\n"
        "Respuesta:"
    ),
}


def generar(modelo: str, prompt: str) -> dict:
    """Una generacion en streaming, midiendo el primer token."""

    cuerpo = json.dumps(
        {
            "model": modelo,
            "prompt": prompt,
            "stream": True,
            "options": {"temperature": 0.0, "num_predict": 80},
        }
    ).encode()

    peticion = urllib.request.Request(
        f"{OLLAMA}/api/generate", data=cuerpo, headers={"Content-Type": "application/json"}
    )

    t0 = time.perf_counter()
    primer_token = None
    texto = []
    final = {}
    with urllib.request.urlopen(peticion, timeout=300) as resp:
        for linea in resp:
            if not linea.strip():
                continue
            evento = json.loads(linea)
            if evento.get("response"):
                if primer_token is None:
                    primer_token = time.perf_counter() - t0
                texto.append(evento["response"])
            if evento.get("done"):
                final = evento
    total = time.perf_counter() - t0

    n_out = final.get("eval_count", len(texto))
    resultado = {
        "ttft_s": round(primer_token or total, 4),
        "total_s": round(total, 4),
        "tokens_out": n_out,
        "tok_s": round(n_out / total, 1) if total > 0 else 0.0,
        "tokens_in": final.get("prompt_eval_count", 0),
        "texto": "".join(texto).strip()[:200],
    }
    return resultado


def calentar(modelo: str) -> None:
    generar(modelo, "hola")


def main() -> int:
    repeticiones = 5
    informe: dict = {"ollama": OLLAMA, "repeticiones": repeticiones, "modelos": {}}

    for modelo in MODELOS:
        print("=" * 78)
        print(f"MODELO: {modelo}")
        print("=" * 78)
        calentar(modelo)
        informe["modelos"][modelo] = {}

        for nombre, prompt in CASOS.items():
            corridas = [generar(modelo, prompt) for _ in range(repeticiones)]
            ttft = [c["ttft_s"] for c in corridas]
            toks = [c["tok_s"] for c in corridas]
            resumen = {
                "ttft_p50_ms": round(statistics.median(ttft) * 1000, 1),
                "ttft_max_ms": round(max(ttft) * 1000, 1),
                "tok_s_mediana": round(statistics.median(toks), 1),
                "tokens_in": corridas[0]["tokens_in"],
                "tokens_out_mediana": round(statistics.median(c["tokens_out"] for c in corridas), 1),
                "muestra_salida": corridas[0]["texto"],
            }
            informe["modelos"][modelo][nombre] = resumen
            print(
                f"  {nombre:20s} TTFT p50={resumen['ttft_p50_ms']:7.1f} ms  "
                f"max={resumen['ttft_max_ms']:7.1f} ms  "
                f"{resumen['tok_s_mediana']:5.1f} tok/s  "
                f"in={resumen['tokens_in']} out={resumen['tokens_out_mediana']:.0f}"
            )
            print(f"       -> {resumen['muestra_salida'][:120]}")
        print()

    destino = Path(__file__).resolve().parents[1] / "docs" / "metrics" / "bench_llm.json"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(informe, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"informe escrito en {destino}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
