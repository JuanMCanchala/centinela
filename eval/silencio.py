"""El paciente se queda callado. Qué hace el agente, medido de verdad.

La rúbrica lo pide dentro de *Calidad de la conversación (voz)*: «la latencia de la
conversación (...) y **qué hace tu solución durante los silencios**». Lo que hacía era
nada: el VAD del navegador no cerraba nunca, el turno quedaba abierto, y la llamada duraba
hasta que el barredor de inactividad la cerraba a los 180 s. Tres minutos de nadie
diciendo nada y un registro clínico que no explicaba por qué.

Este arnés abre una llamada real, dice al servidor que abrió el micrófono, y **no habla**.
Manda tramas de silencio digital durante 45 s -- exactamente lo que manda un navegador
cuyo dueño no contesta -- y anota qué llega de vuelta y cuándo.

Lo que tiene que ocurrir, y en este orden:

    ~6 s   acompaña        «Tómese su tiempo. Sigo aquí.»
    ~14 s  repregunta      el reintento del dominio que estaba abierto
    ~25 s  comprueba       «No le escucho. ¿Sigue ahí?»
    ~40 s  cierra          y deja constancia de que la valoración quedó a medias

Y lo que NO tiene que ocurrir, que es la mitad del valor de esta prueba:

- Nada antes de los 6 s. Pisar la pausa de quien piensa es el error que la literatura de
  agentes de voz nombra por su cuenta, y un paciente en el tercer día de un postoperatorio
  piensa despacio.
- Ni un peldaño repetido. El vigilante mira cada medio segundo: sin el contador que lleva
  la cuenta, «tómese su tiempo» saldría dos veces por segundo.
- Ni una escalada a rojo. Callarse no es un síntoma.

    python -m eval.silencio [--url ws://127.0.0.1:8000]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import httpx
import websockets
from eval.destino import url_http, url_ws

PACIENTE = {
    "paciente_id": "pac_silencio", "nombre": "Prueba Silencio",
    "procedimiento": "Apendicectomía", "dia_postop": 7, "edad": 72, "genero": "F",
    "comorbilidades": ["diabetes"],
}

FRECUENCIA = 16000
MS_TRAMA = 20
# Cuanto se aguanta callado. Bastante mas que los 40 s del ultimo peldano, porque esos 40
# son de SILENCIO: lo que el agente tarda en decir cada peldano no cuenta contra el
# siguiente, asi que la escalera completa ocupa mas tiempo de reloj que de silencio.
#
# El margen es amplio a proposito. Con 62 s la prueba fallaba cuando el servidor venia de
# atender otra suite: la sintesis de cada peldano tarda mas con la maquina ocupada y la
# escalera se estira. En una llamada real eso solo significa que los peldanos llegan un poco
# mas tarde, asi que lo que estaba mal medido era la ventana del arnes, no la conducta.
SEGUNDOS = 78.0

# Silencio digital, que es lo que manda un microfono en una habitacion quieta cuando el
# cliente ya calibro su piso de ruido. No es un tono: un tono lo tomaria el detector de
# interrupcion por voz y esto probaria lo contrario de lo que quiere probar.
TRAMA_MUDA = b"\x00\x00" * int(FRECUENCIA * MS_TRAMA / 1000)


def _minuto(t0: float) -> str:
    return f"{time.perf_counter() - t0:5.1f}s"


async def correr(base_ws: str, base_http: str) -> int:
    async with httpx.AsyncClient(base_url=base_http, timeout=60.0) as cli:
        r = await cli.post("/api/llamadas", json=PACIENTE)
        r.raise_for_status()
        llamada_id = r.json()["llamada_id"]
        print(f"llamada {llamada_id} abierta; el agente saluda y el paciente no contesta")

        # Un turno del paciente, para que la llamada tenga contenido: asi se prueba el
        # cierre por silencio CON contacto, que es el que produce seguimiento. Sin
        # ningun turno el cierre correcto es otro -- `sin_contacto`, fuera de la bandeja
        # clinica -- y son dos conductas distintas que conviene no mezclar.
        await cli.post(f"/api/llamadas/{llamada_id}/turno",
                       json={"texto": "Sí, soy yo"})

        sucesos: list[tuple[float, str, str]] = []
        t0 = time.perf_counter()

        async with websockets.connect(f"{base_ws}/ws/llamada/{llamada_id}") as ws:
            # La senal que arranca el reloj del silencio. La manda el cliente porque el
            # saludo suena por su `<audio>` y no por este socket: ahi el servidor no
            # tiene la palabra y no distingue "callado" de "todavia hablando".
            await ws.send(json.dumps({"tipo": "escuchando"}))

            async def enviar_silencio() -> None:
                fin = time.perf_counter() + SEGUNDOS
                while time.perf_counter() < fin:
                    await ws.send(TRAMA_MUDA)
                    await asyncio.sleep(MS_TRAMA / 1000)

            async def leer() -> None:
                fin = time.perf_counter() + SEGUNDOS
                while time.perf_counter() < fin:
                    resto = fin - time.perf_counter()
                    try:
                        crudo = await asyncio.wait_for(ws.recv(), timeout=max(0.1, resto))
                    except (asyncio.TimeoutError, websockets.ConnectionClosed):
                        break
                    if isinstance(crudo, str):
                        m = json.loads(crudo)
                        tipo = m.get("tipo", "?")
                        if tipo == "fin_voz":
                            # Un cliente real confirma que ya sono, y esa confirmacion es
                            # lo unico que devuelve el suelo al paciente. Sin mandarla,
                            # este arnes veia un solo peldano y parecia un fallo del
                            # servidor: el agente se quedaba con la palabra esperando una
                            # senal que nadie iba a dar.
                            await ws.send(json.dumps({"tipo": "fin_reproduccion"}))
                            await ws.send(json.dumps({"tipo": "escuchando"}))
                        elif tipo == "voz":
                            sucesos.append((time.perf_counter() - t0, "dice",
                                            m.get("texto", "")))
                            print(f"  {_minuto(t0)}  DICE  {m.get('texto', '')[:70]}")
                        elif tipo == "fin_llamada":
                            sucesos.append((time.perf_counter() - t0, "cierra",
                                            m.get("motivo", "")))
                            print(f"  {_minuto(t0)}  CIERRA  motivo={m.get('motivo')}")
                        elif tipo not in ("fin_voz", "turno", "transcripcion"):
                            print(f"  {_minuto(t0)}  {tipo}")

            # El envio de silencio y la lectura van juntos: el cliente real manda tramas
            # siempre, tambien mientras el agente habla.
            await asyncio.gather(enviar_silencio(), leer(), return_exceptions=True)

        # ------------------------------------------------------------------
        estado = await cli.get(f"/api/llamadas/{llamada_id}/traza")
        traza = estado.json() if estado.status_code == 200 else {}

    dichos = [s for s in sucesos if s[1] == "dice"]
    cierres = [s for s in sucesos if s[1] == "cierra"]
    fallos: list[str] = []

    print()
    print("=" * 78)
    print("RESULTADO")
    print("=" * 78)

    # 1. Nada antes de los seis segundos.
    tempranos = [s for s in dichos if s[0] < 5.0]
    if tempranos:
        fallos.append(f"habla a los {tempranos[0][0]:.1f}s: pisa la pausa del paciente")

    # 2. Los cuatro peldanos, en orden.
    if len(dichos) < 4:
        fallos.append(f"solo {len(dichos)} intervenciones; se esperaban 4 peldanos")
    else:
        primero = dichos[0][2].lower()
        if "tiempo" not in primero:
            fallos.append(f"el primer peldano no acompana: «{primero[:50]}»")
        if not any("sigue" in d[2].lower() for d in dichos):
            fallos.append("nunca comprueba la linea")

    # 3. Ni un peldano repetido.
    textos = [d[2] for d in dichos]
    repetidos = [t for t in set(textos) if textos.count(t) > 1]
    if repetidos:
        fallos.append(f"repite un peldano {len(repetidos)} vez/veces: se muerde la cola")

    # 4. Cierra, y con el motivo que explica lo que paso.
    if not cierres:
        fallos.append("no cierra la llamada: se queda abierta hasta el barredor")
    elif cierres[0][2] != "silencio_del_paciente":
        fallos.append(f"cierra con motivo «{cierres[0][2]}», no por silencio")

    # 5. Y no escala a rojo por callarse.
    nivel = (traza.get("decision") or {}).get("nivel") or traza.get("nivel_final")
    if nivel == "rojo":
        fallos.append("escala a ROJO por un silencio: callarse no es un sintoma")

    print(f"  intervenciones      : {len(dichos)}")
    for t, _que, texto in dichos:
        print(f"    {t:5.1f}s  {texto[:66]}")
    print(f"  cierre              : {cierres[0][2] if cierres else 'NINGUNO'}")
    print(f"  nivel final         : {nivel}")
    print()

    if fallos:
        print("  FALLOS:")
        for f in fallos:
            print(f"    - {f}")
        codigo = 1
    else:
        print("  El agente acompana el silencio, no lo pisa, y deja constancia al cerrar.")
        codigo = 0
    return codigo


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=url_ws())
    ap.add_argument("--http", default=url_http())
    a = ap.parse_args()
    return asyncio.run(correr(a.url.rstrip("/"), a.http.rstrip("/")))


if __name__ == "__main__":
    raise SystemExit(main())
