"""Comprueba cuando el pipeline invoca el modelo y cuando no.

Existe porque la prueba de humo reportaba 0 tokens y 0 invocaciones en todos los
turnos, y hacia falta distinguir dos explicaciones muy distintas:

  (a) la instrumentacion no propaga el consumo -- seria un bug;
  (b) el modelo de verdad no se invoca porque las capas de reglas resolvieron el
      turno -- seria el diseno funcionando.

Para separarlas se mandan turnos de las dos clases: unos que las reglas resuelven
solas (numero explicito, lexico conocido) y otros deliberadamente ambiguos, donde
el modelo es la unica via.

    python -m eval.probar_tokens [--url http://127.0.0.1:8000]
"""

from __future__ import annotations

import argparse

import httpx

PACIENTE = {
    "paciente_id": "pac_tokens", "nombre": "Prueba de Consumo",
    "procedimiento": "Apendicectomía", "dia_postop": 7, "edad": 40, "genero": "M",
    "comorbilidades": [],
}

# (texto, se_espera_invocacion_del_modelo, por_que)
TURNOS = (
    ("Si, soy yo", None, "confirmacion de identidad"),
    ("El dolor es un 4", False,
     "numero explicito: la regex lo resuelve, no hace falta el modelo"),
    ("La herida se ve rojita alrededor", False,
     "lexico conocido: 'rojita' esta en PISTAS_HERIDA"),
    ("Pues mire, esa molestia va y viene, no sabria decirle un numero exacto", True,
     "ambiguo: ni numero ni lexico conocido, el modelo es la unica via"),
    ("Ando como raro del estomago desde antier, no se explicar bien", True,
     "descripcion indirecta sin termino del lexico"),
    ("He comido normal", False, "lexico conocido: 'como normal' esta en PISTAS_APETITO"),
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    args = ap.parse_args()

    c = httpx.Client(base_url=args.url, timeout=180.0)
    ini = c.post("/api/llamadas", json=PACIENTE)
    ini.raise_for_status()
    lid = ini.json()["llamada_id"]
    print(f"llamada {lid[:8]}\n")

    print(f"{'turno':52s} {'in':>5s} {'out':>5s} {'inv':>4s} {'ms':>7s}  esperado")
    print("-" * 96)

    fallos = []
    for texto, espera_modelo, por_que in TURNOS:
        r = c.post(f"/api/llamadas/{lid}/turno", json={"texto": texto})
        r.raise_for_status()
        d = r.json()
        m = d["metricas_turno"]
        inv = m["invocaciones_llm"]

        if espera_modelo is None:
            veredicto = "-"
        elif espera_modelo and inv > 0:
            veredicto = "OK"
        elif not espera_modelo and inv == 0:
            veredicto = "OK"
        else:
            veredicto = "FALLA"
            fallos.append((texto, espera_modelo, inv, por_que))

        print(f"{texto[:52]:52s} {m['tokens_entrada']:5d} {m['tokens_salida']:5d} "
              f"{inv:4d} {m['ms_hasta_primer_audio']:7.0f}  {veredicto}")

    tot = c.get("/api/metricas").json()["resumen"]
    print()
    print(f"acumulado de la llamada: "
          f"{tot['tokens_por_llamada']['entrada_media']:.0f} tokens entrada, "
          f"{tot['tokens_por_llamada']['salida_media']:.0f} salida")
    print(f"invocaciones al modelo por turno: p50={tot['invocaciones_llm_por_turno']['p50']}, "
          f"max={tot['invocaciones_llm_por_turno']['max']}")
    lat = tot["latencia_hasta_primer_audio"]
    print(f"latencia hasta primer audio: p50={lat['p50_ms']} ms, p95={lat['p95_ms']} ms")

    if fallos:
        print("\nFALLOS:")
        for texto, espera, inv, por_que in fallos:
            print(f"  {texto[:60]}")
            print(f"    esperaba invocacion={espera}, hubo {inv}. {por_que}")

    codigo = 1 if fallos else 0
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
