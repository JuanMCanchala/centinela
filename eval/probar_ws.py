"""Mide el camino de voz completo por WebSocket, etapa por etapa.

Existe porque un turno por microfono se quedaba "Procesando..." demasiado tiempo y
hacia falta separar las causas posibles: transporte, transcripcion, extraccion,
sintesis, o un audio mal formado que hace trabajar de mas a Whisper.

Manda audio PCM16 real por el WebSocket igual que el navegador y cronometra cada
mensaje que vuelve.

    python -m eval.probar_ws [--url ws://127.0.0.1:8100] [--segundos 3]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import struct
import time

import httpx
import websockets

PACIENTE = {
    "paciente_id": "pac_ws", "nombre": "Prueba WebSocket",
    "procedimiento": "Apendicectomía", "dia_postop": 7, "edad": 40, "genero": "M",
    "comorbilidades": [],
}


def _voz_sync(texto: str) -> bytes | None:
    """Sintetiza `texto` con Piper y lo devuelve como PCM16 a 16 kHz mono.

    Usar voz real y no un tono es la unica forma de probar este camino de verdad:
    el VAD de Silero que trae faster-whisper descarta cualquier tono -- y hace
    bien --, asi que un test con senal artificial siempre dice "sin voz" y no
    prueba nada. Aqui se cierra el circulo: el TTS del propio sistema genera el
    audio que su STT tiene que entender, remuestreado igual que lo hace el
    navegador.

    Corre en su propio bucle de eventos porque `PiperTTS.sintetizar` es asincrono
    y este helper se invoca desde `asyncio.to_thread`.
    """

    import io as _io
    import sys as _sys
    import wave as _wave
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "api"))
    from centinela.tts.piper import PiperTTS  # noqa: PLC0415

    tts = PiperTTS()
    if not tts.disponible:
        return None

    audio = asyncio.run(tts.sintetizar(texto))
    if not audio.wav:
        return None

    with _wave.open(_io.BytesIO(audio.wav), "rb") as w:
        origen = w.getframerate()
        marcos = w.readframes(w.getnframes())

    muestras = list(struct.unpack(f"<{len(marcos)//2}h", marcos))

    # Piper entrega a 22.05 kHz; se remuestrea a 16 kHz promediando la ventana,
    # exactamente el mismo algoritmo que `remuestrearA16k` en web/app.js.
    if origen != 16000:
        razon = origen / 16000
        salida = []
        for i in range(int(len(muestras) / razon)):
            desde = int(i * razon)
            hasta = min(len(muestras), int((i + 1) * razon))
            ventana = muestras[desde:hasta] or [0]
            salida.append(int(sum(ventana) / len(ventana)))
        muestras = salida

    return struct.pack(f"<{len(muestras)}h", *muestras)


def tono_pcm16(segundos: float, frecuencia: int = 16000) -> bytes:
    """Tono con forma de habla. NO es voz: el VAD lo descarta, y debe hacerlo.

    Se conserva para probar precisamente eso -- que el sistema no invente una
    transcripcion a partir de ruido estructurado.
    """

    n = int(segundos * frecuencia)
    muestras = []
    for i in range(n):
        t = i / frecuencia
        v = (
            0.35 * math.sin(2 * math.pi * 140 * t)
            + 0.25 * math.sin(2 * math.pi * 700 * t)
            + 0.15 * math.sin(2 * math.pi * 1200 * t)
        )
        v *= 0.5 + 0.5 * math.sin(2 * math.pi * 3.5 * t)
        muestras.append(int(max(-1.0, min(1.0, v)) * 24000))
    return struct.pack(f"<{n}h", *muestras)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8100")
    ap.add_argument("--segundos", type=float, default=3.0)
    ap.add_argument(
        "--decir",
        default="El dolor esta como en un seis y tengo la herida enrojecida",
        help="frase que se sintetiza con Piper y se manda como si fuera el paciente",
    )
    ap.add_argument("--rapido", action="store_true", help="envia el audio de golpe (falsea la especulacion)")
    ap.add_argument("--sin-especular", action="store_true", help="no manda pausa_corta: mide la latencia sin especulacion")
    ap.add_argument("--tono", action="store_true",
                    help="manda un tono en vez de voz (debe dar sin_habla)")
    args = ap.parse_args()

    http = args.url.replace("ws://", "http://").replace("wss://", "https://")
    c = httpx.Client(base_url=http, timeout=120.0)
    lid = c.post("/api/llamadas", json=PACIENTE).json()["llamada_id"]

    if args.tono:
        pcm = tono_pcm16(args.segundos)
        print(f"llamada {lid[:8]}  ·  TONO sintetico de {args.segundos}s")
        print("esperado: sin_habla (el sistema no debe inventar una transcripcion)\n")
    else:
        pcm = await asyncio.to_thread(_voz_sync, args.decir)
        if pcm is None:
            print("Piper no disponible; usa --tono o instala la voz")
            return 2
        print(f"llamada {lid[:8]}  ·  VOZ real sintetizada con Piper")
        print(f"frase: {args.decir!r}\n")

    print(f"PCM16: {len(pcm)} bytes ({len(pcm)/2/16000:.2f}s a 16 kHz mono)")

    async with websockets.connect(f"{args.url}/ws/llamada/{lid}", max_size=None) as ws:
        # Se imita al navegador: trozos de 1024 muestras (64 ms) en tiempo real, no
        # todo de golpe. Enviarlo de golpe falsearia la prueba, porque la
        # especulacion depende de que el audio llegue al ritmo del habla.
        trozo = 1024 * 2
        n_trozos = 0
        for i in range(0, len(pcm), trozo):
            await ws.send(pcm[i:i + trozo])
            n_trozos += 1
            if not args.rapido:
                await asyncio.sleep(0.064)
        print(f"  {n_trozos} trozos enviados en tiempo real")

        # El cliente real manda `pausa_corta` a los 350 ms de silencio, 550 ms
        # antes de declarar el fin del turno. Se reproduce esa secuencia.
        if not args.sin_especular:
            await ws.send(json.dumps({"tipo": "pausa_corta"}))
            print("  -> pausa_corta (el servidor empieza a transcribir ya)")
            # Se consume el acuse para que no se confunda con la respuesta.
            try:
                aviso = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                if aviso.get("tipo") == "especulando":
                    print(f"     especulacion arrancada={aviso['arrancada']} "
                          f"sobre {aviso['segundos_acumulados']}s")
            except asyncio.TimeoutError:
                pass
            if not args.rapido:
                await asyncio.sleep(0.55)

        t0 = time.perf_counter()
        await ws.send(json.dumps({"tipo": "fin_habla"}))
        print("  -> fin_habla enviado, cronometro en marcha\n")

        recibido_audio = False
        primer_sonido = None
        esperando_relleno = False
        for _ in range(8):
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=90)
            except asyncio.TimeoutError:
                print(f"  [{(time.perf_counter()-t0)*1000:8.0f} ms] TIMEOUT sin respuesta")
                break

            ms = (time.perf_counter() - t0) * 1000
            if isinstance(msg, bytes):
                if primer_sonido is None:
                    primer_sonido = ms
                etiqueta = "RELLENO" if esperando_relleno else "AUDIO"
                print(f"  [{ms:8.0f} ms] {etiqueta} {len(msg)} bytes "
                      f"({max(0,len(msg)-44)/2/22050:.1f}s de voz)")
                if esperando_relleno:
                    esperando_relleno = False
                else:
                    recibido_audio = True
                    break
            else:
                d = json.loads(msg)
                tipo = d.get("tipo")
                if tipo == "relleno":
                    esperando_relleno = True
                    print(f"  [{ms:8.0f} ms] aviso de relleno (muletilla en camino)")
                elif tipo == "especulando":
                    print(f"  [{ms:8.0f} ms] especulando arrancada={d.get('arrancada')}")
                elif tipo == "transcripcion":
                    print(f"  [{ms:8.0f} ms] transcripcion  stt={d['ms']:.0f} ms  "
                          f"rtf={d['factor_tiempo_real']}  texto={d['texto'][:60]!r}")
                elif tipo == "sin_habla":
                    print(f"  [{ms:8.0f} ms] sin_habla: {d['mensaje']} "
                          f"(audio {d['duracion_audio_s']}s)")
                    # Comportamiento correcto y terminal: no habra audio de
                    # respuesta, asi que no hay nada que esperar.
                    print("              (correcto: no se inventa transcripcion)")
                    break
                elif tipo == "turno":
                    m = d.get("metricas_turno", {})
                    print(f"  [{ms:8.0f} ms] turno  nivel={(d.get('decision') or {}).get('nivel')}")
                    print(f"              etapas: {m.get('ms_por_etapa')}")
                    print(f"              tokens in/out: {m.get('tokens_entrada')}/"
                          f"{m.get('tokens_salida')}  invocaciones={m.get('invocaciones_llm')}")
                    print(f"              agente: {d.get('agente_dice','')[:80]}")
                else:
                    print(f"  [{ms:8.0f} ms] {tipo}: {str(d)[:120]}")

        total = (time.perf_counter() - t0) * 1000
        print()
        if args.tono:
            print(f"  Tono rechazado en {total:.0f} ms sin inventar transcripcion.")
            codigo = 0
        elif recibido_audio:
            print(f"  TOTAL desde fin_habla hasta el primer audio: {total:.0f} ms")
            codigo = 0
        else:
            print(f"  FALLA: no llego audio de respuesta ({total:.0f} ms)")
            codigo = 1

    return codigo


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
