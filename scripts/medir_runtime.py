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

# Por debajo de esto la muestra no da para publicar percentiles. Es un aviso, no
# un error: puede que se este midiendo a proposito una sola llamada.
MINIMO_TURNOS = 20


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
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

        salida = Path(args.salida)
        salida.parent.mkdir(parents=True, exist_ok=True)
        salida.write_text(
            json.dumps(datos, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        lat = resumen.get("latencia_hasta_primer_audio", {})
        print(f"metricas de ejecucion congeladas en {salida.relative_to(RAIZ)}")
        print(f"  muestra                : {n_turnos} turnos en {n_llamadas} llamadas")
        print(f"  latencia P50 / P95     : {lat.get('p50_ms')} ms / {lat.get('p95_ms')} ms")
        print(f"  definicion             : {lat.get('definicion', '(sin declarar)')}")

        if n_turnos < MINIMO_TURNOS:
            print()
            print(f"AVISO: {n_turnos} turnos es poca muestra para publicar P95.")
            print("       Corra `python -m eval.humo` antes de medir.")

    return codigo


if __name__ == "__main__":
    sys.exit(main())
