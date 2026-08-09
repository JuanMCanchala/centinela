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

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "api"))
from centinela.dialog.policy import MAX_REPETICIONES_SEGUIDAS  # noqa: E402

DOMINIOS_CLINICOS = ("dolor_nrs", "fiebre_c", "movilidad", "herida", "apetito", "sueno")


def _dominios_resueltos(estado: dict) -> int:
    """Cuantos de los seis dominios tienen ya un valor.

    Es la senal de que la conversacion AVANZA. Una fiebre negada cuenta: el
    paciente respondio, aunque no haya temperatura que guardar.
    """

    n = 0
    for campo in DOMINIOS_CLINICOS:
        obs = estado.get(campo) or {}
        if obs.get("conocido") and obs.get("valor") is not None:
            n += 1
    if estado.get("fiebre_negada") and not (estado.get("fiebre_c") or {}).get("conocido"):
        n += 1
    return n


from eval.escucha import DIR_AUDIOS, leer_wav  # noqa: E402
from eval.probar_ws import _voz_sync  # noqa: E402

# La misma llamada del guion sintetico, pero con las grabaciones humanas. Los
# nombres son los de `eval/escucha.py`; la frase de al lado es lo que se dijo, para
# que el informe pueda comparar.
GUION_GRABADO = (
    ("Si soy yo", "01_si_soy_yo.wav"),
    ("Un seis", "03_un_seis.wav"),
    ("Treinta y siete cinco", "07_treinta_y_siete_cinco.wav"),
    ("Camino normal", "16_camino_normal.wav"),
    ("La herida tiene un liquido amarillo espeso y huele mal", "11_liquido_amarillo.wav"),
)


def _pcm_de_wav(ruta) -> bytes:
    """WAV -> PCM16 a 16 kHz, tal como lo manda el navegador por el WebSocket."""

    import numpy as _np

    muestras = leer_wav(ruta)
    return _np.clip(muestras * 32768.0, -32768, 32767).astype("<i2").tobytes()

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

    resultado: dict = {"ms_primer_sonido": None, "ms_turno": None, "tipos_recibidos": []}
    esperando_relleno = False

    # El turno se lee hasta que el servidor dice que acabo de hablar (`fin_voz`), no
    # hasta un numero fijo de mensajes.
    #
    # Antes eran diez, y ese limite hacia que el arnes MINTIERA. Un turno del agente son
    # mas de diez mensajes -- relleno, transcripcion, turno y un trozo de audio por
    # fragmento -- asi que el bucle salia con la cola sin leer, y esos bytes se
    # interpretaban como la respuesta del turno SIGUIENTE. El sintoma era espectacular y
    # apuntaba al sitio equivocado: el sistema parecia ir un turno por detras -- se le
    # decia "treinta y siete cinco" y contestaba a "un seis" -- cuando lo que iba
    # atrasado era el lector. El tope alto de abajo es solo una red contra un bucle
    # infinito, no un criterio de fin de turno.
    for _ in range(4000):
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
        else:
            d = json.loads(msg)
            tipo = d.get("tipo")
            resultado["tipos_recibidos"].append(tipo)
            if tipo in ("fin_voz", "fin_llamada"):
                # El servidor declara que termino de hablar. Aqui acaba el turno, y con
                # el socket drenado el siguiente empieza limpio.
                break
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
    ap.add_argument("--audios", action="store_true",
                    help="usa las grabaciones humanas de eval/audios/ en vez de Piper")
    args = ap.parse_args()

    http = args.url.replace("ws://", "http://").replace("wss://", "https://")
    c = httpx.Client(base_url=http, timeout=180.0)
    ini = c.post("/api/llamadas", json=PACIENTE).json()
    lid = ini["llamada_id"]

    print(f"llamada {lid[:8]} — caso ROJO del dataset, por voz\n")
    print(f"  AGENTE: {ini['agente_dice'][:96]}...\n")

    audios = []
    if args.audios:
        # Voz humana grabada, la de eval/audios/. Es la prueba que de verdad
        # importa: Piper hablandole a su propio Whisper valida la tuberia, no la
        # escucha.
        print("usando las grabaciones de voz humana de eval/audios/...")
        for frase, archivo in GUION_GRABADO:
            ruta = DIR_AUDIOS / archivo
            if not ruta.exists():
                print(f"falta {ruta.name}; corra `make escucha-guion` para ver que grabar")
                return 2
            audios.append((frase, _pcm_de_wav(ruta)))
    else:
        print("sintetizando los turnos con Piper...")
        for frase in GUION:
            pcm = await asyncio.to_thread(_voz_sync, frase)
            if pcm is None:
                print("Piper no disponible")
                return 2
            audios.append((frase, pcm))
    print(f"{len(audios)} turnos listos\n")

    fallos: list[str] = []
    # Turnos en los que el agente no emitio audio. Se cuentan aparte de `fallos` porque
    # no siempre son un defecto del sistema: este arnes manda el turno siguiente sin
    # esperar a que el agente termine de hablar, asi que puede llegar dentro de la
    # ventana del barge-in. Contarlos es lo que permite distinguir una cosa de la otra.
    sin_respuesta: list[int] = []
    dominios: list[str | None] = []
    # Cuantos dominios habia resueltos tras cada turno. Es lo que distingue una
    # pregunta de profundizacion de un atasco de verdad.
    resueltos: list[int] = []
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
            else:
                print(f"  oido      : {r.get('oido')!r}")
                print(f"  intencion : {r.get('intencion')}   nivel: {r.get('nivel')}")
                print(f"  dominio   : {r.get('dominio')}")
                print(f"  AGENTE    : {(r.get('agente_dice') or '')[:96]}")

                # Un turno puede no producir audio del agente, y entonces no hay latencia
                # que imprimir. Pasaba con `--audios`: el arnes revienta con un TypeError
                # al formatear None y se pierde el diagnostico justo del turno raro, que
                # es el unico que interesaba. Decir que no hubo respuesta es el dato.
                if r.get("ms_primer_sonido") is None or r.get("ms_turno") is None:
                    print(f"  latencia  : el agente no emitio audio en este turno "
                          f"(mensajes recibidos: {r.get('tipos_recibidos') or 'ninguno'})")
                    sin_respuesta.append(i)
                else:
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
                resueltos.append(_dominios_resueltos(r.get("estado_clinico") or {}))
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
    # tiene que AVANZAR, no repetir la misma pregunta sin entender.
    #
    # Contar "el mismo dominio dos veces seguidas" mide lo que no debe. El agente
    # repite dominio por dos motivos distintos y solo uno es un defecto:
    #
    #   - ATASCO: no entendio y vuelve a preguntar lo mismo. El estado clinico no
    #     avanza. Esto es el fallo.
    #   - PROFUNDIZACION: entendio, y hace una pregunta de seguimiento dentro del
    #     mismo dominio -- "y ese dolor, le cede con las pastillas?". El estado SI
    #     avanzo. Esto es comportamiento diseñado.
    #
    # La primera version daba por atasco los dos casos y fallaba en una llamada
    # perfectamente sana: "Un seis" resolvia dolor=6 y el agente profundizaba.
    # Lo que distingue un caso del otro es si el estado clinico crecio.
    atascos = 0
    for i in range(1, len(dominios)):
        mismo = dominios[i] is not None and dominios[i] == dominios[i - 1]
        avanzo = resueltos[i] > resueltos[i - 1]
        if mismo and not avanzo:
            atascos += 1

    print(f"  dominios recorridos : {dominios}")
    print(f"  dominios resueltos por turno: {resueltos}")
    print(f"  repeticiones sin avance (atascos): {atascos}")

    if sin_respuesta:
        print(f"  turnos sin audio del agente: {sin_respuesta} de {len(audios)}")
        print("    Este arnes no espera a que el agente termine de hablar antes de mandar")
        print("    el turno siguiente, asi que un turno puede caer dentro de la ventana de")
        print("    barge-in y contar como interrupcion. Es el precio de medir a ritmo fijo.")
        print("    Lo que NO puede pasar es que el estado clinico deje de avanzar, y eso lo")
        print("    mide la cuenta de atascos de arriba.")

    # El contrato lo fija `DialogPolicy.MAX_REPETICIONES_SEGUIDAS`: tras ese numero
    # de intentos, el agente sigue adelante. Se compara contra la constante y no
    # contra un numero a mano para que las dos no puedan divergir.
    if atascos > MAX_REPETICIONES_SEGUIDAS:
        fallos.append(
            f"la conversacion se atasco: {atascos} repeticiones sin avance, "
            f"por encima del maximo de diseño ({MAX_REPETICIONES_SEGUIDAS})"
        )

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
