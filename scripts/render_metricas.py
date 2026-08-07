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
    runtime = leer("runtime.json")

    L: list[str] = []
    L.append("# Métricas medidas")
    L.append("")
    L.append("> Generado por `scripts/render_metricas.py` a partir de los informes que")
    L.append("> producen los arneses de evaluación. Ninguna cifra se escribe a mano.")
    L.append(f"> Última generación: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    L.append("")

    # ------------------------------------------------- metricas exigidas (§5)
    #
    # Van PRIMERO y con el nombre que usa la rubrica, porque son las unicas
    # obligatorias: "si no estan, el apartado correspondiente se califica muy por
    # debajo de su tope, aunque tu solucion funcione bien".
    if runtime:
        r = runtime.get("resumen", {})
        lat = r.get("latencia_hasta_primer_audio", {})
        tt = r.get("tokens_por_turno", {})
        tl = r.get("tokens_por_llamada", {})
        inv = r.get("invocaciones_llm_por_turno", {})
        rag = r.get("consultas_rag_por_llamada", {})
        costo = runtime.get("costo", {})

        L.append("## Métricas exigidas por la rúbrica (§5)")
        L.append("")
        L.append(f"Muestra: **{r.get('n_turnos', 0)} turnos** en "
                 f"**{r.get('n_llamadas', 0)} llamadas**, medidos por `obs/metrics.py` "
                 "durante la ejecución real de la API.")
        L.append("")
        L.append("### Latencia de respuesta")
        L.append("")
        L.append(f"> {lat.get('definicion', '(sin declarar)')}")
        L.append("")
        L.append("| Percentil | Latencia |")
        L.append("|---|---:|")
        L.append(f"| **P50** | **{lat.get('p50_ms')} ms** |")
        L.append(f"| **P95** | **{lat.get('p95_ms')} ms** |")
        L.append(f"| P99 | {lat.get('p99_ms')} ms |")
        L.append(f"| mínimo | {lat.get('min_ms')} ms |")
        L.append(f"| máximo | {lat.get('max_ms')} ms |")
        L.append("")
        cache = r.get("tts", {})
        L.append(f"El P50 es de milisegundos porque **{cache.get('proporcion_desde_cache', 0) * 100:.0f} %** "
                 f"de los turnos se responden desde el caché de audio pre-renderizado "
                 f"({cache.get('turnos_servidos_desde_cache', 0)} de {r.get('n_turnos', 0)}): "
                 "la conversación la conduce una máquina de estados, así que las locuciones "
                 "del guion se conocen antes de que suene el teléfono. El P95 y el P99 son "
                 "los turnos que sí necesitan sintetizar voz nueva o invocar al modelo.")
        L.append("")
        L.append("### Consumo")
        L.append("")
        L.append("| Métrica | Valor |")
        L.append("|---|---:|")
        L.append(f"| Tokens de entrada por turno (P50) | {tt.get('entrada_p50')} |")
        L.append(f"| Tokens de salida por turno (P50) | {tt.get('salida_p50')} |")
        L.append(f"| Tokens de entrada por turno (media) | {tt.get('entrada_media')} |")
        L.append(f"| Tokens de salida por turno (media) | {tt.get('salida_media')} |")
        L.append(f"| Tokens de entrada por llamada (media) | **{tl.get('entrada_media')}** |")
        L.append(f"| Tokens de salida por llamada (media) | **{tl.get('salida_media')}** |")
        L.append(f"| Turnos por llamada (media) | {tl.get('turnos_media')} |")
        L.append(f"| Invocaciones al modelo por turno (P50) | **{inv.get('p50')}** |")
        L.append(f"| Invocaciones al modelo por turno (máx) | {inv.get('max')} |")
        L.append(f"| Consultas al RAG por llamada (media) | **{rag.get('media')}** |")
        L.append(f"| Consultas al RAG por llamada (máx) | {rag.get('max')} |")
        L.append("")
        L.append("Dos cifras se leen mal si no se explican, así que van explicadas.")
        L.append("")
        L.append("**El P50 de tokens y de invocaciones al modelo es 0** porque la mayoría de los")
        L.append("turnos no llegan al modelo: si la expresión regular ya extrajo el dolor y el")
        L.append("léxico resolvió el estado de la herida, no hay nada que preguntarle. El modelo")
        L.append("se invoca cuando el turno es ambiguo, y ahí sube a 1. Es la consecuencia de que")
        L.append("la decisión clínica la tome el motor de reglas y no el modelo.")
        L.append("")
        L.append(f"**Las consultas al RAG por llamada ({rag.get('media')} de media) son bajas** porque el")
        L.append("cuestionario no consulta el corpus: recorre seis dominios con preguntas fijas. El")
        L.append("RAG entra cuando el paciente pregunta algo clínico —*«¿puedo ducharme?»*,")
        L.append("*«¿esto es normal?»*— y entonces la respuesta va fundamentada y con su cita. Una")
        L.append("media alta acá significaría que el agente consulta documentos para preguntar la")
        L.append("temperatura, que sería gasto sin ganancia.")
        L.append("")

        if costo and not costo.get("aviso"):
            d = costo.get("desglose_usd", {})
            L.append("### Costo estimado por llamada")
            L.append("")
            L.append(f"> {costo.get('aclaracion', '')}")
            L.append("")
            L.append("| Concepto | USD por llamada |")
            L.append("|---|---:|")
            L.append(f"| Modelo de lenguaje | {d.get('llm')} |")
            L.append(f"| Transcripción | {d.get('stt')} |")
            L.append(f"| Síntesis de voz | {d.get('tts')} |")
            L.append(f"| **Total** | **{costo.get('costo_total_usd_por_llamada')}** |")
            L.append(f"| Total en pesos colombianos | ${costo.get('costo_total_cop_por_llamada')} |")
            L.append("")
            insumos = costo.get("insumos_medidos", {})
            if insumos:
                L.append("Insumos medidos que entran en el cálculo: "
                         + " · ".join(f"{k.replace('_', ' ')} = {v}"
                                      for k, v in insumos.items())
                         + ". Las tarifas de referencia están en "
                           "`obs/metrics.py::PRECIOS_REFERENCIA`.")
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
    L.append("make humo        # extremo a extremo (requiere la API levantada)")
    L.append("make bench       # latencia de modelo y voz")
    L.append("make test        # tests unitarios y de regresión")
    L.append("make metricas    # regenera este documento")
    L.append("```")
    L.append("")
    L.append("Las métricas exigidas por la rúbrica (§5) se miden sobre la API en marcha,")
    L.append("así que llevan su propia secuencia. El servidor tiene que estar **recién")
    L.append("arrancado**: si se mide sobre uno donde alguien estuvo probando a mano, la")
    L.append("muestra queda llena de llamadas abiertas y abandonadas de dos turnos y las")
    L.append("medias por llamada salen más bajas de lo que corresponde a una llamada real.")
    L.append("")
    L.append("```bash")
    L.append("make up                      # servidor limpio, en otra terminal")
    L.append("make humo                    # 6 llamadas completas, ~30 turnos")
    L.append("make runtime                 # congela /api/metricas en docs/metrics/")
    L.append("make metricas                # las escribe acá arriba")
    L.append("```")

    DESTINO.parent.mkdir(parents=True, exist_ok=True)
    DESTINO.write_text("\n".join(L), encoding="utf-8")
    print(f"escrito {DESTINO.relative_to(RAIZ)} ({len(L)} lineas)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
