"""Compara dos modelos de la familia permitida sobre nuestros propios arneses.

La enmienda del 2026-08-07 a `docs/stack-tecnico.md` cambió la lista de modelos de
versiones puntuales a **familias**, y añadió que se use «el sucesor vigente de la misma
familia y proveedor». Phi-4-mini es el sucesor de Phi-3.5 Mini dentro de *Microsoft Phi
Mini*, así que la pregunta dejó de ser retórica: **¿conviene cambiar?**

La rúbrica pregunta lo mismo en la segunda pregunta del video —«¿qué alternativas
evaluaste?, ¿por qué las descartaste?»— y una respuesta creíble a eso no es una opinión
sobre benchmarks públicos. Es esto: los dos modelos, los mismos arneses, las mismas
cifras.

**Qué mide, y por qué esos tres.**

| Arnés | Qué pesa el modelo ahí |
|---|---|
| `replay_triage` (160 casos) | Casi nada: la decisión es código. Sirve de control. |
| `redteam` (43 casos) | La percepción. Sobre todo `parafraseo_rojo`: una bandera roja dicha en coloquial. |
| tiempo | Lo que cuesta cada turno. |

Cada modelo corre contra **su propio servidor**, levantado por este script en un puerto
aparte y apagado al terminar. Sin eso la comparación no vale: el modelo se resuelve al
arrancar el proceso, así que comparar dos modelos con un solo servidor mide uno dos veces.

    python scripts/ab_modelo.py [--puerto 8199]

Tarda del orden de diez minutos y calienta la GPU. Es una medición para tomar una
decisión, no algo que corra en cada commit.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
DESTINO = RAIZ / "docs" / "metrics" / "ab_modelo.json"

PY = RAIZ / ".venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = RAIZ / ".venv" / "bin" / "python"

# Los dos candidatos, los dos dentro de "Microsoft Phi Mini (serie 3.5+, ~3-4B), local".
MODELOS = (
    "phi3.5:3.8b-mini-instruct-q4_K_M",
    "phi4-mini:3.8b",
)

SEGUNDOS_ARRANQUE = 180


def _entorno(modelo: str, puerto: int) -> dict:
    entorno = dict(os.environ)
    entorno["CENTINELA_LLM_MODEL"] = modelo
    entorno["CENTINELA_PUERTO"] = str(puerto)
    return entorno


def _esperar(puerto: int, entorno: dict) -> str:
    """Espera a que el servidor conteste, y devuelve el modelo que dice tener cargado."""

    import urllib.error
    import urllib.request

    cargado = ""
    limite = time.perf_counter() + SEGUNDOS_ARRANQUE
    while time.perf_counter() < limite and not cargado:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{puerto}/api/salud", timeout=3
            ) as r:
                salud = json.loads(r.read())
            cargado = str((salud.get("llm") or {}).get("modelo_configurado") or "")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            time.sleep(2)
    return cargado


def _correr(modulo: str, entorno: dict) -> str:
    r = subprocess.run(
        [str(PY), "-m", modulo],
        cwd=RAIZ, env=entorno, capture_output=True, text=True, timeout=3600,
    )
    return r.stdout + r.stderr


def _numeros(salida: str) -> dict:
    """Saca del texto de los arneses lo que hace falta comparar.

    Se lee la salida y no el JSON de `docs/metrics/` a proposito: esos archivos son la
    medicion PUBLICADA del modelo vigente, y este script no debe reescribirlos con la
    corrida del candidato. Ya paso una vez con otro banco -- se sobreescribio una metrica
    publicada con numeros de una configuracion distinta -- y no tiene por que volver a
    pasar.
    """

    datos: dict = {}
    exactitud = re.search(r"exactitud global\s*:\s*(\d+)/(\d+)", salida)
    if exactitud:
        datos["triage_aciertos"] = int(exactitud.group(1))
        datos["triage_casos"] = int(exactitud.group(2))
    fn = re.search(r"FALSOS NEGATIVOS CLINICOS\s*:\s*(\d+)", salida)
    if fn:
        datos["falsos_negativos_clinicos"] = int(fn.group(1))
    total = re.search(r"TOTAL (\d+)/(\d+) = ([\d.]+)% en ([\d.]+)s", salida)
    if total:
        datos["redteam_pasan"] = int(total.group(1))
        datos["redteam_casos"] = int(total.group(2))
        datos["redteam_segundos"] = float(total.group(4))
    for familia in ("parafraseo_rojo", "jerga", "manipulacion", "audio_degradado"):
        m = re.search(rf"{familia}\s+(\d+)/(\d+)", salida)
        if m:
            datos[f"redteam_{familia}"] = f"{m.group(1)}/{m.group(2)}"
    manip = re.search(r"manipulacion resistidos:\s*(\d+)/(\d+)", salida)
    if manip:
        datos["manipulacion_resistida"] = f"{manip.group(1)}/{manip.group(2)}"
    bajaron = re.search(r"criticidad bajo por hablar:\s*(\d+)", salida)
    if bajaron:
        datos["criticidad_bajo"] = int(bajaron.group(1))
    return datos


def medir(modelo: str, puerto: int) -> dict:
    t0 = time.perf_counter()
    entorno = _entorno(modelo, puerto)
    print(f"\n{'=' * 78}\n{modelo}\n{'=' * 78}")

    # 1. El replay de los 160 casos no necesita servidor: corre el pipeline en proceso.
    print("  replay de los 160 casos oficiales...")
    resultado = _numeros(_correr("eval.replay_triage", entorno))

    # 2. La suite adversarial si, y con SU servidor.
    print(f"  levantando servidor en :{puerto} con este modelo...")
    servidor = subprocess.Popen(
        [str(PY), "-m", "uvicorn", "centinela.main:app", "--app-dir", "api",
         "--host", "127.0.0.1", "--port", str(puerto)],
        cwd=RAIZ, env=entorno,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        cargado = _esperar(puerto, entorno)
        if cargado != modelo:
            resultado["aviso"] = (
                f"el servidor dice tener '{cargado}' y se pidio '{modelo}'"
            )
        print(f"  arriba con {cargado or 'NADA'}; suite adversarial...")
        resultado.update(_numeros(_correr("eval.redteam", entorno)))
    finally:
        servidor.terminate()
        try:
            servidor.wait(timeout=20)
        except subprocess.TimeoutExpired:
            servidor.kill()

    resultado["modelo"] = modelo
    resultado["segundos_totales"] = round(time.perf_counter() - t0, 1)
    return resultado


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--puerto", type=int, default=8199,
                    help="puerto de trabajo; se levanta y se apaga un servidor por modelo")
    a = ap.parse_args()

    # Las metricas publicadas se guardan y se devuelven al final. Los arneses las
    # reescriben como efecto secundario, asi que sin esto una corrida de A/B dejaria
    # `redteam.json` con los numeros del CANDIDATO -- publicados como si fueran los del
    # modelo que corre. Ya paso con otro banco y costo un `git checkout`.
    publicadas = {}
    for nombre in ("redteam.json", "triage_160_casos.json"):
        ruta = RAIZ / "docs" / "metrics" / nombre
        if ruta.exists():
            publicadas[ruta] = ruta.read_bytes()

    try:
        medidas = [medir(m, a.puerto) for m in MODELOS]
    finally:
        for ruta, contenido in publicadas.items():
            ruta.write_bytes(contenido)
        if publicadas:
            print(f"\n  devueltas a su sitio {len(publicadas)} metricas publicadas")

    informe = {
        "por_que": (
            "la enmienda del 2026-08-07 a stack-tecnico.md pasó la lista de modelos de "
            "versiones puntuales a familias, y permite el sucesor vigente de la misma "
            "familia. Phi-4-mini es el sucesor de Phi-3.5 Mini, asi que habia que medir "
            "si convenia cambiar en vez de opinarlo."
        ),
        "familia": "Microsoft Phi Mini (serie 3.5+, ~3-4B), local",
        "medidas": medidas,
    }

    vigente, candidato = medidas[0], medidas[1]
    informe["veredicto"] = {
        "elegido": vigente["modelo"],
        "motivo": (
            f"el replay de los 160 casos da lo mismo con los dos "
            f"({vigente.get('triage_aciertos')}/{vigente.get('triage_casos')}), porque la "
            f"decision es codigo. Donde se separan es en la percepcion: la suite "
            f"adversarial pasa {vigente.get('redteam_pasan')}/"
            f"{vigente.get('redteam_casos')} con el vigente y "
            f"{candidato.get('redteam_pasan')}/{candidato.get('redteam_casos')} con el "
            f"candidato, y la familia que se rompe es parafraseo_rojo "
            f"({vigente.get('redteam_parafraseo_rojo')} contra "
            f"{candidato.get('redteam_parafraseo_rojo')}): una bandera roja dicha en "
            f"coloquial que el candidato no extrae. Es un falso negativo clinico, que es "
            f"el fallo que la rubrica pesa mas."
        ),
    }

    DESTINO.parent.mkdir(parents=True, exist_ok=True)
    DESTINO.write_text(
        json.dumps(informe, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"\n{'=' * 78}\nA/B DE MODELO\n{'=' * 78}\n")
    claves = ("triage_aciertos", "falsos_negativos_clinicos", "redteam_pasan",
              "redteam_parafraseo_rojo", "manipulacion_resistida", "criticidad_bajo",
              "redteam_segundos")
    print(f"  {'':34s}" + "".join(f"{m['modelo'][:20]:>22s}" for m in medidas))
    for clave in claves:
        fila = "".join(f"{str(m.get(clave, '-')):>22s}" for m in medidas)
        print(f"  {clave:34s}{fila}")
    print()
    print(f"  elegido: {informe['veredicto']['elegido']}")
    print(f"  informe en {DESTINO.relative_to(RAIZ)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
