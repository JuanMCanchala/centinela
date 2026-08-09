"""Congela las metricas de ejecucion de la API en `docs/metrics/runtime.json`.

Por que existe este script.

La rubrica exige en su §5 cuatro cifras concretas, y son obligatorias: latencia
P50 y P95 *desde que el paciente termina de hablar hasta que empieza a sonar el
audio*, tokens de entrada y salida por turno **y por llamada**, invocaciones al
modelo por turno, y consultas al RAG por llamada.

`obs/metrics.py` las mide todas desde el principio, pero solo vivian en memoria
del proceso y se servian por `/api/metricas`. En cuanto el servidor se reiniciaba
desaparecian, asi que el README las contaba a mano y en otra forma -- desglose por
etapa en vez de P50/P95 de la metrica que la rubrica nombra. Reportar en una forma
distinta de la exigida es un hueco, y la rubrica avisa de que lo que se reporta se
contrasta con los logs de la sesion.

Este script cierra ese hueco: deja las cifras medidas en disco para que
`render_metricas.py` las escriba en `docs/metricas.md`, igual que ya hace con el
motor de decision y la suite adversarial. Ninguna cifra del README se escribe a
mano.

Secuencia de medicion (la que documenta el README):

    1. levantar el servidor recien arrancado, para que el contador este limpio
    2. python -m eval.humo --url http://127.0.0.1:8000     (6 llamadas, ~30 turnos)
    3. python scripts/medir_runtime.py --url http://127.0.0.1:8000

El paso 1 importa. Si se mide sobre un servidor donde alguien estuvo probando a
mano, la muestra queda llena de llamadas abiertas y abandonadas de dos turnos, y
las medias por llamada salen mas bajas de lo que corresponde a una llamada real.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
DESTINO = RAIZ / "docs" / "metrics" / "runtime.json"
HISTORICO = RAIZ / "data" / "runtime" / "metricas.jsonl"

# El destino se decide en un solo sitio para todos los arneses; este vive en `scripts/`,
# asi que hay que poner la raiz en la ruta de importacion antes de pedirlo.
sys.path.insert(0, str(RAIZ))

from eval.destino import url_http  # noqa: E402

# Por debajo de esto la muestra no da para publicar percentiles. Es un aviso, no
# un error: puede que se este midiendo a proposito una sola llamada.
MINIMO_TURNOS = 20

# Y por debajo de esto no se publica el P95 del camino de VOZ.
#
# El guardia de arriba contaba turnos totales, y los de texto son la mayoria: aprobaba
# con 42 turnos cuando el camino de voz tenia 13 muestras. Un P99 sobre 13 muestras es
# el maximo con decimales.
MINIMO_TURNOS_DE_VOZ = 20

# Lo que el cliente tarda en declarar que el paciente callo. NO esta dentro de
# `ms_hasta_primer_audio`: esa medicion arranca cuando el VAD cierra.
#
# Se publica aparte y sumado, porque el mercado mide su "time to first audio byte" desde
# que el llamante deja de hablar, endpointing incluido. Publicar solo la mitad de despues
# es comparar contra otra linea de salida.
#
# Los dos numeros son `web/app.js`: 900 ms de techo, y 450 ms cuando la respuesta ya
# resuelve el dominio preguntado. `eval/escucha.py` mide cuantas de las 18 grabaciones
# humanas cierran pronto -- 11 de 18 en la ultima corrida.
MS_ENDPOINTING_TECHO = 900.0
MS_ENDPOINTING_ADAPTATIVO = 450.0


def _percentil(valores: list[float], p: float) -> float | None:
    import math

    calculado = None
    if valores:
        v = sorted(valores)
        k = (len(v) - 1) * p / 100.0
        bajo = math.floor(k)
        alto = min(bajo + 1, len(v) - 1)
        calculado = round(v[bajo] + (v[alto] - v[bajo]) * (k - bajo), 1)
    return calculado


def _resumir(turnos: list[dict], etiqueta: str, definicion: str) -> dict:
    lat = [
        f["ms_hasta_primer_audio"] for f in turnos
        if isinstance(f.get("ms_hasta_primer_audio"), (int, float))
    ]
    return {
        "camino": etiqueta,
        "definicion": definicion,
        "n": len(lat),
        "p50_ms": _percentil(lat, 50),
        "p95_ms": _percentil(lat, 95),
        "p99_ms": _percentil(lat, 99),
        "max_ms": round(max(lat), 1) if lat else None,
    }


def por_camino(ruta: Path) -> dict:
    """El histórico completo, partido por el camino que siguió cada turno.

    **Por qué existe.** La cifra que se publicaba era el P95 de la ventana en memoria del
    proceso vivo: 42 turnos, casi todos preguntas del guion servidas desde el cache de
    audio. Salía 1433 ms y no describía nada que le pase a un paciente, porque mezcla dos
    poblaciones que difieren en cuatro órdenes de magnitud -- una locución cacheada sale
    en 0.6 ms y un turno que consulta el corpus tarda segundos. Mientras tanto había
    miles de turnos medidos en disco que nadie leía.

    Una sola cifra sobre esa mezcla se mueve con la proporción de preguntas del guion que
    tuviera la muestra, no con el sistema. Partido por camino, cada número dice de qué
    habla.
    """

    resultado: dict = {"disponible": False}
    if ruta.exists():
        turnos = []
        for linea in ruta.read_text(encoding="utf-8").splitlines():
            if linea.strip():
                try:
                    turnos.append(json.loads(linea))
                except json.JSONDecodeError:
                    pass

        def con_etapa(f: dict, etapa: str) -> bool:
            return etapa in (f.get("ms_por_etapa") or {})

        voz = [f for f in turnos if con_etapa(f, "stt")]
        # Relativa cuando se puede, absoluta cuando no: el historico puede estar fuera
        # del repo (`CENTINELA_DIR_RUNTIME` lo permite) y `relative_to` lanza ahi.
        try:
            fuente = str(ruta.relative_to(RAIZ))
        except ValueError:
            fuente = str(ruta)

        resultado = {
            "disponible": True,
            "fuente": fuente.replace("\\", "/"),
            "n_turnos_medidos": len(turnos),
            "caminos": [
                _resumir(turnos, "todos",
                         "cualquier turno, de voz o de texto"),
                _resumir([f for f in turnos if f.get("tts_desde_cache")],
                         "voz del agente desde cache",
                         "pregunta del guion: la locucion ya estaba sintetizada"),
                _resumir([f for f in turnos if not f.get("tts_desde_cache")],
                         "voz del agente sintetizada en el turno",
                         "el agente dijo algo que no estaba en el guion"),
                _resumir([f for f in turnos if (f.get("invocaciones_llm") or 0) > 0],
                         "con invocacion al modelo",
                         "el regex no resolvio y hubo que preguntarle al LLM"),
                _resumir([f for f in turnos if (f.get("consultas_rag") or 0) > 0],
                         "con consulta al corpus",
                         "el paciente pregunto algo que se responde con la guia"),
                _resumir(voz, "turno de voz (entro por el microfono)",
                         "el unico camino que un paciente recorre de verdad"),
            ],
            "muestra_de_voz_suficiente": len(voz) >= MINIMO_TURNOS_DE_VOZ,
        }

        # Y partido por configuracion de transcripcion, porque el historico cruza
        # cambios de sistema. Mover el STT de `small/cpu` a `medium/cuda` bajo el p50
        # del camino de voz de 410 ms a 134 ms; un percentil sobre las dos poblaciones
        # juntas no describe ninguna de las dos. Los turnos sin el campo son anteriores
        # a registrarlo y se publican como tales.
        configuraciones = sorted({(f.get("stt") or "sin registrar") for f in voz})
        resultado["voz_por_configuracion"] = [
            _resumir([f for f in voz if (f.get("stt") or "sin registrar") == cfg],
                     f"voz con STT {cfg}",
                     "turnos de voz medidos con esa configuracion de transcripcion")
            for cfg in configuraciones
        ]
        # La vigente es la del final del historico: lo que mide el sistema de hoy.
        resultado["configuracion_vigente"] = (
            (voz[-1].get("stt") or "sin registrar") if voz else None
        )
    return resultado


def extremo_a_extremo(caminos: dict) -> dict:
    """La latencia contada desde que el paciente calla, que es donde el mercado la cuenta.

    `ms_hasta_primer_audio` arranca cuando el VAD del cliente declara fin de habla, y eso
    deja fuera el endpointing. No es un error de medicion -- la rubrica nombra ese
    instante -- pero comparar ese numero con el "time to first audio byte" de una
    plataforma comercial es comparar contra otra linea de salida, unos cientos de
    milisegundos mas adelante. Aqui se publica la suma, con el sumando a la vista.
    """

    # La configuracion vigente, no la mezcla de todo el historico: publicar el p95 de
    # una poblacion que incluye el sistema anterior describiria un sistema que ya no
    # existe. Si no hay campo de configuracion, se cae al camino de voz completo y se
    # dice cual se uso.
    vigente = caminos.get("configuracion_vigente")
    voz = None
    for c in caminos.get("voz_por_configuracion", []):
        if vigente and c["camino"] == f"voz con STT {vigente}":
            voz = c
    if voz is None:
        for c in caminos.get("caminos", []):
            if c["camino"].startswith("turno de voz"):
                voz = c

    resumen: dict = {"disponible": False}
    if voz and voz["p50_ms"] is not None:
        resumen = {
            "disponible": True,
            "definicion": (
                "desde que el paciente deja de hablar hasta el primer byte de audio del "
                "agente, endpointing incluido. Es la definicion con la que se publican "
                "los benchmarks de agentes de voz."
            ),
            "endpointing_ms": {
                "adaptativo": MS_ENDPOINTING_ADAPTATIVO,
                "techo": MS_ENDPOINTING_TECHO,
                "nota": (
                    "el turno cierra a los 450 ms cuando la respuesta ya resuelve el "
                    "dominio preguntado, y espera el techo de 900 ms cuando no. "
                    "`eval/escucha.py` mide cuantas grabaciones cierran pronto."
                ),
            },
            "p50_ms_con_cierre_adaptativo": round(
                voz["p50_ms"] + MS_ENDPOINTING_ADAPTATIVO, 1),
            "p50_ms_con_techo": round(voz["p50_ms"] + MS_ENDPOINTING_TECHO, 1),
            "p95_ms_con_techo": round(voz["p95_ms"] + MS_ENDPOINTING_TECHO, 1),
            "n": voz["n"],
            "medido_sobre": voz["camino"],
        }
    return resumen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=url_http())
    ap.add_argument("--salida", default=str(DESTINO))
    args = ap.parse_args()

    url = f"{args.url.rstrip('/')}/api/metricas"
    codigo = 0

    try:
        with urllib.request.urlopen(url, timeout=30) as respuesta:
            datos = json.loads(respuesta.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"No se pudo leer {url}: {exc}")
        print("Levante la API primero (make up) y vuelva a intentarlo.")
        codigo = 1
    else:
        resumen = datos.get("resumen", {})
        n_turnos = resumen.get("n_turnos", 0)
        n_llamadas = resumen.get("n_llamadas", 0)

        # El historico de disco, que es la muestra de verdad: la ventana en memoria solo
        # tiene lo que haya pasado por ESTE proceso.
        caminos = por_camino(HISTORICO)
        datos["por_camino"] = caminos
        datos["extremo_a_extremo"] = extremo_a_extremo(caminos)

        salida = Path(args.salida)
        salida.parent.mkdir(parents=True, exist_ok=True)
        salida.write_text(
            json.dumps(datos, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        lat = resumen.get("latencia_hasta_primer_audio", {})
        print(f"metricas de ejecucion congeladas en {salida.relative_to(RAIZ)}")
        print(f"  ventana en memoria     : {n_turnos} turnos en {n_llamadas} llamadas")
        print(f"  latencia P50 / P95     : {lat.get('p50_ms')} ms / {lat.get('p95_ms')} ms")
        print(f"  definicion             : {lat.get('definicion', '(sin declarar)')}")

        if caminos.get("disponible"):
            print()
            print(f"  historico en disco     : {caminos['n_turnos_medidos']} turnos")
            for c in caminos["caminos"]:
                print(f"    {c['camino']:<42} n={c['n']:<5} "
                      f"p50={c['p50_ms']} ms  p95={c['p95_ms']} ms")

        if caminos.get("voz_por_configuracion"):
            print()
            print("  el camino de voz, partido por configuracion de STT:")
            for c in caminos["voz_por_configuracion"]:
                print(f"    {c['camino']:<42} n={c['n']:<5} "
                      f"p50={c['p50_ms']} ms  p95={c['p95_ms']} ms")
            print(f"    vigente: {caminos.get('configuracion_vigente')}")

        e2e = datos["extremo_a_extremo"]
        if e2e.get("disponible"):
            print()
            print(f"  desde que el paciente calla, sobre {e2e['medido_sobre']}:")
            print(f"    con cierre adaptativo (450 ms) : "
                  f"{e2e['p50_ms_con_cierre_adaptativo']} ms P50")
            print(f"    con el techo (900 ms)          : "
                  f"{e2e['p50_ms_con_techo']} ms P50 / "
                  f"{e2e['p95_ms_con_techo']} ms P95")

        if n_turnos < MINIMO_TURNOS:
            print()
            print(f"AVISO: {n_turnos} turnos es poca muestra para publicar P95.")
            print("       Corra `python -m eval.humo` antes de medir.")

        # El aviso que faltaba. El de arriba cuenta turnos totales, y los de texto son
        # mayoria: aprobaba con 42 turnos cuando el camino de voz tenia 13 muestras.
        if caminos.get("disponible") and not caminos["muestra_de_voz_suficiente"]:
            n_voz = next(
                (c["n"] for c in caminos["caminos"] if c["camino"].startswith("turno de voz")),
                0,
            )
            print()
            print(f"AVISO: solo {n_voz} turnos de VOZ en el historico "
                  f"(hacen falta {MINIMO_TURNOS_DE_VOZ}).")
            print("       Corra `python -m eval.conversacion_voz` unas veces.")

    return codigo


if __name__ == "__main__":
    sys.exit(main())
