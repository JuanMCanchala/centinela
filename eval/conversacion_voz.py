"""Una llamada COMPLETA por voz, turno a turno, como la haria un paciente.

Existe porque las pruebas anteriores median un turno aislado, y el fallo que el
usuario encontro solo aparecia en el segundo: el primer turno pasaba y el segundo
se quedaba atascado. Un test de un turno no lo habria visto nunca.

Cada turno se sintetiza con Piper, se manda por el WebSocket en tiempo real con su
`pausa_corta` correspondiente, y se comprueba que la conversacion AVANZA -- que el
agente no repite el mismo dominio dos veces seguidas.

    python -m eval.conversacion_voz [--url ws://127.0.0.1:8100]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import httpx
import websockets

from eval.probar_ws import _voz_sync

PACIENTE = {
    "paciente_id": "pac_42_00026", "nombre": "Ana Lucia Restrepo",
    "procedimiento": "Colecistectomía", "dia_postop": 7, "edad": 47, "genero": "F",
    "comorbilidades": ["diabetes_tipo_2"], "ciudad": "Medellín", "eps": "Sura EPS",
}

# El guion del caso rojo del dataset, con las respuestas cortas que rompian la
# conversacion. "Si soy yo" y "Un seis" son exactamente los turnos que el sistema
# marcaba como audio degradado.
GUION = (
    "Si soy yo",
    "Un seis",
    "Si, me tome la temperatura y estaba en treinta y siete cinco",
    "Camino normal",
    "La herida tiene un liquido amarillo saliendo y huele feo",
)


async def turno(ws, pcm: bytes, rapido: bool) -> dict:
    """Manda un turno de voz completo y devuelve la respuesta del agente."""

    trozo = 1024 * 2
    for i in range(0, len(pcm), trozo):
        await ws.send(pcm[i:i + trozo])
        if not rapido:
            await asyncio.sleep(0.064)

    # Igual que el navegador: pausa corta primero, fin de turno despues.
    await ws.send(json.dumps({"tipo": "pausa_corta"}))
    if not rapido:
        await asyncio.sleep(0.4)
    t0 = time.perf_counter()
    await ws.send(json.dumps({"tipo": "fin_habla"}))

    resultado: dict = {"ms_primer_sonido": None, "ms_turno": None}
    esperando_relleno = False

    for _ in range(10):
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=60)
        except asyncio.TimeoutError:
            resultado["timeout"] = True
            break

        ms = (time.perf_counter() - t0) * 1000

        if isinstance(msg, bytes):
            if resultado["ms_primer_sonido"] is None:
                resultado["ms_primer_sonido"] = ms
            if esperando_relleno:
                esperando_relleno = False
            elif resultado.get("agente_dice"):
                break
        else:
            d = json.loads(msg)
            tipo = d.get("tipo")
            if tipo == "relleno":
                esperando_relleno = True
            elif tipo == "transcripcion":
                resultado["oido"] = d["texto"]
                resultado["ms_stt"] = d["ms"]
                resultado["origen"] = d.get("origen")
            elif tipo == "turno":
                resultado["ms_turno"] = ms
                resultado["agente_dice"] = d["agente_dice"]
                resultado["intencion"] = d.get("intencion_detectada")
                resultado["dominio"] = d.get("dominio_actual")
                resultado["nivel"] = (d.get("decision") or {}).get("nivel")
                resultado["terminada"] = d.get("terminada")
                resultado["escala"] = d.get("escala_ahora")
                resultado["estado_clinico"] = d.get("estado_clinico")
            elif tipo == "sin_habla":
                resultado["sin_habla"] = d.get("mensaje")
                break
            elif tipo == "especulando":
                pass
            elif tipo == "error":
                resultado["error"] = d.get("mensaje")
                break

    return resultado


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8100")
    ap.add_argument("--rapido", action="store_true")
    args = ap.parse_args()

    http = args.url.replace("ws://", "http://").replace("wss://", "https://")
    c = httpx.Client(base_url=http, timeout=180.0)
    ini = c.post("/api/llamadas", json=PACIENTE).json()
    lid = ini["llamada_id"]

    print(f"llamada {lid[:8]} — caso ROJO del dataset, por voz\n")
    print(f"  AGENTE: {ini['agente_dice'][:96]}...\n")

    print("sintetizando los turnos con Piper...")
    audios = []
    for frase in GUION:
        pcm = await asyncio.to_thread(_voz_sync, frase)
        if pcm is None:
            print("Piper no disponible")
            return 2
        audios.append((frase, pcm))
    print(f"{len(audios)} turnos listos\n")

    fallos: list[str] = []
    dominios: list[str | None] = []
    latencias: list[float] = []
    escalado = False

    async with websockets.connect(f"{args.url}/ws/llamada/{lid}", max_size=None) as ws:
        for i, (frase, pcm) in enumerate(audios, start=1):
            print("=" * 88)
            print(f"[{i}/{len(audios)}] PACIENTE dice: {frase!r}")
            r = await turno(ws, pcm, args.rapido)

            if r.get("timeout"):
                print("  TIMEOUT: el agente no respondio")
                fallos.append(f"turno {i} sin respuesta ({frase!r})")
                break

            if r.get("sin_habla"):
                print(f"  sin_habla: {r['sin_habla']}")
                fallos.append(f"turno {i} descartado como sin voz ({frase!r})")
                continue

            if r.get("error"):
                print(f"  error: {r['error']}")
                fallos.append(f"turno {i}: {r['error']}")
                continue

            print(f"  oido      : {r.get('oido')!r}")
            print(f"  intencion : {r.get('intencion')}   nivel: {r.get('nivel')}")
            print(f"  dominio   : {r.get('dominio')}")
            print(f"  AGENTE    : {(r.get('agente_dice') or '')[:96]}")
            print(f"  latencia  : primer sonido {r['ms_primer_sonido']:.0f} ms · "
                  f"turno {r['ms_turno']:.0f} ms · stt {r.get('ms_stt', 0):.0f} ms "
                  f"({r.get('origen')})")

            if r.get("intencion") == "audio_degradado":
                fallos.append(
                    f"turno {i}: {frase!r} se clasifico como AUDIO DEGRADADO "
                    f"aunque se transcribio como {r.get('oido')!r}"
                )
                print("  FALLA: marcado como audio degradado con transcripcion correcta")

            dominios.append(r.get("dominio"))
            if r.get("ms_turno"):
                latencias.append(r["ms_turno"])
            if r.get("escala"):
                escalado = True
                print(f"  ESCALA: nivel {r.get('nivel')}")
            if r.get("terminada"):
                print("  (llamada terminada por el agente)")
                break

    # ------------------------------------------------------------------
    print()
    print("=" * 88)
    print("RESULTADO")
    print("=" * 88)

    # La comprobacion que el fallo del usuario habria pillado: la conversacion
    # tiene que avanzar de dominio, no repetir el mismo.
    repetidos = sum(1 for a, b in zip(dominios, dominios[1:]) if a == b and a is not None)
    print(f"  dominios recorridos : {dominios}")
    print(f"  repeticiones seguidas del mismo dominio: {repetidos}")
    if repetidos > 1:
        fallos.append(f"la conversacion se atasco: {repetidos} repeticiones de dominio")

    if latencias:
        print(f"  latencia por turno  : min {min(latencias):.0f} ms · "
              f"media {sum(latencias)/len(latencias):.0f} ms · max {max(latencias):.0f} ms")
    print(f"  escalo a rojo       : {escalado}")

    print()
    if fallos:
        print("  FALLOS:")
        for f in fallos:
            print(f"    - {f}")
        codigo = 1
    else:
        print("  La conversacion completa por voz avanza sin atascarse.")
        codigo = 0
    return codigo


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
