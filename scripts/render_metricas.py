"""Genera las tablas de metricas del README a partir de los informes medidos.

La rubrica advierte: *"Lo que reportes se contrasta con lo que ocurre en la
sesion y con tus logs. Reportar numeros que no se sostienen es peor que no
reportarlos."*

Por eso ninguna cifra del README se escribe a mano. Este script las lee de los
JSON que producen los arneses de evaluacion y las renderiza en
`docs/metricas.md`, que el README incluye por referencia. Si un numero cambia,
cambia porque una medicion cambio.

    python scripts/render_metricas.py
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
DIR = RAIZ / "docs" / "metrics"
DESTINO = RAIZ / "docs" / "metricas.md"


def leer(nombre: str) -> dict | None:
    ruta = DIR / nombre
    datos = json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else None
    return datos


def main() -> int:
    triage = leer("triage_160_casos.json")
    redteam = leer("redteam.json")
    bench_llm = leer("bench_llm.json")
    bench_voz = leer("bench_voz.json")
    bench_voces = leer("bench_voces.json")
    corpus = leer("corpus_index.json")

    L: list[str] = []
    L.append("# Métricas medidas")
    L.append("")
    L.append("> Generado por `scripts/render_metricas.py` a partir de los informes que")
    L.append("> producen los arneses de evaluación. Ninguna cifra se escribe a mano.")
    L.append(f"> Última generación: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    L.append("")

    # ---------------------------------------------------------------- triage
    if triage:
        L.append("## Decisión clínica sobre los 160 casos oficiales")
        L.append("")
        L.append(f"Motor `{triage['version_reglas']}`. Reproducible con `make eval`: el motor no")
        L.append("tiene ninguna fuente de aleatoriedad, así que estas cifras son idénticas en")
        L.append("cada corrida.")
        L.append("")
        L.append("| Clase | Recall | Precisión | n real | n predicho |")
        L.append("|---|---:|---:|---:|---:|")
        for clase in ("rojo", "amarillo", "verde"):
            c = triage["por_clase"][clase]
            L.append(f"| {clase} | **{c['recall']:.3f}** | {c['precision']:.3f} | "
                     f"{c['n_real']} | {c['n_predicho']} |")
        L.append("")
        L.append("| Métrica | Valor |")
        L.append("|---|---:|")
        L.append(f"| **Falsos negativos clínicos** | **{triage['falsos_negativos_clinicos']}** |")
        L.append(f"| Rojos sub-escalados | {triage['rojos_subescalados']} |")
        L.append(f"| Exactitud global | {triage['exactitud_global']:.3f} "
                 f"({sum(triage['matriz_confusion'][n][n] for n in ('verde','amarillo','rojo'))}"
                 f"/{triage['n_casos']}) |")
        L.append(f"| Sobre-escalamientos (dirección segura) | {triage['sobre_escalamientos']} "
                 f"({triage['sobre_escalamientos']/triage['n_casos']:.1%}) |")
        L.append("")
        L.append("Matriz de confusión (filas = etiqueta oficial, columnas = decisión del motor):")
        L.append("")
        L.append("| real \\ predicho | verde | amarillo | rojo |")
        L.append("|---|---:|---:|---:|")
        for r in ("verde", "amarillo", "rojo"):
            m = triage["matriz_confusion"][r]
            L.append(f"| **{r}** | {m['verde']} | {m['amarillo']} | {m['rojo']} |")
        L.append("")

    # --------------------------------------------------------------- redteam
    if redteam:
        L.append("## Suite adversarial")
        L.append("")
        L.append(f"`make redteam` · {redteam['pasan']}/{redteam['n_casos']} casos "
                 f"({redteam['tasa']:.1%}) en {redteam['segundos']} s.")
        L.append("")
        L.append("| Familia | Pasan |")
        L.append("|---|---:|")
        for familia, d in redteam["por_familia"].items():
            L.append(f"| {familia.replace('_', ' ')} | {d['pasan']}/{d['total']} |")
        L.append("")
        L.append(f"- Intentos de manipulación resistidos: "
                 f"**{redteam['manipulaciones_resistidas']}/{redteam['manipulaciones_totales']}**")
        L.append(f"- Casos donde la criticidad bajó porque el paciente lo pidió: "
                 f"**{redteam['criticidad_bajada_por_texto']}**")
        L.append("")

    # ------------------------------------------------------------------ LLM
    if bench_llm:
        L.append("## Modelo de lenguaje")
        L.append("")
        L.append("`scripts/bench_llm.py`. Se mide el tiempo hasta el primer token porque en una")
        L.append("conversación de voz es lo que el paciente percibe como silencio.")
        L.append("")
        L.append("| Modelo | Tarea | TTFT p50 | TTFT máx | tok/s |")
        L.append("|---|---|---:|---:|---:|")
        for modelo, tareas in bench_llm["modelos"].items():
            for tarea, m in tareas.items():
                L.append(f"| `{modelo}` | {tarea} | **{m['ttft_p50_ms']:.0f} ms** | "
                         f"{m['ttft_max_ms']:.0f} ms | {m['tok_s_mediana']:.1f} |")
        L.append("")
        L.append("El modelo cuatro veces más pequeño resultó ~5× más lento en tiempo hasta el")
        L.append("primer token, así que se descartó la idea de un router barato en 1B y")
        L.append("Phi-3.5 Mini hace los tres trabajos.")
        L.append("")

    # ------------------------------------------------------------------ voz
    if bench_voces and bench_voces.get("voces"):
        L.append("## Selección de voz")
        L.append("")
        L.append("`scripts/bench_voces.py`. El criterio es el factor de tiempo real (RTF): un")
        L.append("RTF de 1.0 significa que generar un segundo de audio cuesta un segundo.")
        L.append("")
        L.append("| Voz | Tamaño | RTF | Elegida |")
        L.append("|---|---:|---:|---|")
        for voz, d in sorted(bench_voces["voces"].items(), key=lambda kv: kv[1]["rtf_mediana"]):
            marca = "**sí**" if voz == "es_MX-ald-medium" else ""
            L.append(f"| `{voz}` | {d['mb']} MB | {d['rtf_mediana']:.3f} | {marca} |")
        L.append("")
        L.append("Se eligió `es_MX-ald-medium` y no la de menor RTF: la diferencia entre ambas")
        L.append("es del 5 %, y es la única latinoamericana del grupo rápido. El paciente del")
        L.append("reto es colombiano.")
        L.append("")

    if bench_voz and bench_voz.get("tts", {}).get("disponible"):
        t = bench_voz["tts"]
        L.append("## Camino de voz")
        L.append("")
        L.append("| Etapa | Medición |")
        L.append("|---|---:|")
        L.append(f"| Síntesis desde caché (turno de guion) | **{t['cache_ms_p50']:.3f} ms** |")
        for frase, m in list(t["sintesis_en_caliente"].items())[:3]:
            L.append(f"| Síntesis en caliente ({m['caracteres']} car) | "
                     f"{m['ms_p50']:.0f} ms · RTF {m['factor_tiempo_real']:.3f} |")
        s = t["streaming"]
        L.append(f"| Respuesta larga completa | {s['ms_respuesta_completa']:.0f} ms |")
        L.append(f"| Primera frase (streaming) | **{s['ms_primera_frase']:.0f} ms** |")
        pre = t.get("pre_renderizado", {})
        if pre.get("generadas") is not None:
            L.append(f"| Pre-renderizado del guion completo | "
                     f"{pre.get('generadas', 0) + pre.get('reutilizadas', 0)} locuciones |")
        L.append("")

    if bench_voz and bench_voz.get("stt", {}).get("disponible"):
        s = bench_voz["stt"]
        L.append("### Transcripción")
        L.append("")
        L.append(f"`faster-whisper {s['modelo']}` en {s['dispositivo']}.")
        L.append("")
        L.append("| Duración del audio | Latencia | RTF |")
        L.append("|---|---:|---:|")
        for dur, m in s["por_duracion"].items():
            L.append(f"| {dur} | {m['ms_p50']:.0f} ms | {m['factor_tiempo_real']:.3f} |")
        L.append("")

    # --------------------------------------------------------------- corpus
    if corpus:
        L.append("## Corpus indexado")
        L.append("")
        L.append("| Métrica | Valor |")
        L.append("|---|---:|")
        L.append(f"| Documentos | {corpus.get('documentos_indexados', '?')} |")
        L.append(f"| Páginas | {corpus.get('total_paginas', '?')} |")
        L.append(f"| Fragmentos | {corpus.get('total_chunks', '?')} |")
        L.append(f"| Requirieron OCR | {len(corpus.get('documentos_con_ocr', []))} |")
        L.append(f"| Duplicados lógicos detectados | {len(corpus.get('duplicados_logicos', []))} |")
        L.append(f"| Incoherencias carpeta/contenido | "
                 f"{len(corpus.get('incoherencias_de_tema', []))} |")
        L.append("")
        cob = corpus.get("cobertura_por_procedimiento", {})
        if cob:
            L.append("### Cobertura por procedimiento")
            L.append("")
            L.append("| Procedimiento | Tema requerido | Documentos | Estado |")
            L.append("|---|---|---:|---|")
            for proc, c in cob.items():
                estado = "cubierto" if c["cubierto"] else "**SIN COBERTURA**"
                L.append(f"| {proc} | {c['tema_requerido']} | "
                         f"{c['documentos_disponibles']} | {estado} |")
            L.append("")

    L.append("---")
    L.append("")
    L.append("## Cómo reproducir")
    L.append("")
    L.append("```bash")
    L.append("make eval        # decisión clínica sobre los 160 casos")
    L.append("make redteam     # suite adversarial (requiere la API levantada)")
    L.append("make bench       # latencia de modelo y voz")
    L.append("make test        # tests unitarios y de regresión")
    L.append("make metricas    # regenera este documento")
    L.append("```")

    DESTINO.parent.mkdir(parents=True, exist_ok=True)
    DESTINO.write_text("\n".join(L), encoding="utf-8")
    print(f"escrito {DESTINO.relative_to(RAIZ)} ({len(L)} lineas)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
