"""Prueba el respaldo por HTTP del turno de voz.

El navegador usa el WebSocket cuando esta disponible y este endpoint cuando no.
Los dos tienen que dar exactamente el mismo resultado, porque comparten el
pipeline; este script lo comprueba mandando la misma voz por HTTP.

    python -m eval.probar_audio_http [--url http://127.0.0.1:8100]
"""

from __future__ import annotations

import argparse
import time

import httpx

from eval.probar_ws import PACIENTE, _voz_sync, tono_pcm16

FRASES = (
    "Si soy yo",
    "El dolor esta como en un seis",
    "Tengo la herida con un liquido amarillo saliendo",
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8100")
    args = ap.parse_args()

    c = httpx.Client(base_url=args.url, timeout=180.0)
    lid = c.post("/api/llamadas", json=PACIENTE).json()["llamada_id"]
    print(f"llamada {lid[:8]} · turnos de voz por HTTP\n")

    fallos: list[str] = []

    for frase in FRASES:
        pcm = _voz_sync(frase)
        if pcm is None:
            print("Piper no disponible")
            return 2

        t0 = time.perf_counter()
        r = c.post(
            f"/api/llamadas/{lid}/audio",
            content=pcm,
            headers={"Content-Type": "application/octet-stream"},
        )
        ms = (time.perf_counter() - t0) * 1000
        r.raise_for_status()
        d = r.json()

        print(f"  dicho   : {frase!r}  ({len(pcm)/2/16000:.2f}s)")
        if d["tipo"] == "turno":
            t = d["transcripcion"]
            nivel = (d.get("decision") or {}).get("nivel")
            print(f"  oido    : {t['texto']!r}")
            print(f"  nivel   : {nivel}   ({ms:.0f} ms de ida y vuelta)")
            print(f"  agente  : {d['agente_dice'][:88]}")
            if d.get("audio_bytes"):
                print(f"  audio   : {d['audio_bytes']} bytes en {d['audio_url']}")
            else:
                fallos.append(f"sin audio de respuesta para {frase!r}")
        else:
            print(f"  {d['tipo']}: {d.get('mensaje')}")
            fallos.append(f"{d['tipo']} para {frase!r}")
        print()

    # El tono debe rechazarse igual que por WebSocket.
    r = c.post(
        f"/api/llamadas/{lid}/audio",
        content=tono_pcm16(2.0),
        headers={"Content-Type": "application/octet-stream"},
    )
    d = r.json()
    print(f"  tono sintetico -> {d['tipo']}: {d.get('mensaje')}")
    if d["tipo"] != "sin_habla":
        fallos.append("el tono no se rechazo como sin_habla")

    # Audio vacio: no debe reventar.
    r = c.post(
        f"/api/llamadas/{lid}/audio",
        content=b"",
        headers={"Content-Type": "application/octet-stream"},
    )
    d = r.json()
    print(f"  audio vacio    -> {d['tipo']}: {d.get('mensaje')}")
    if d["tipo"] not in ("sin_habla", "error"):
        fallos.append("el audio vacio no se manejo con gracia")

    print()
    if fallos:
        print("FALLOS:")
        for f in fallos:
            print(f"  - {f}")
        codigo = 1
    else:
        print("El respaldo por HTTP funciona igual que el WebSocket.")
        codigo = 0
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
