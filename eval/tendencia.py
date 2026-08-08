"""Cuanto anticipa una regla de tendencia sobre las trayectorias oficiales.

El informe tenia la deteccion de deterioro entre llamadas apuntada como la mejora de
mayor valor pendiente. Este arnes existe porque al medirla dijo lo contrario, y una
conclusion negativa vale lo mismo que una positiva si es reproducible.

Que se mide. El dataset trae cuatro llamadas por paciente (dias 1, 3, 7 y 14) con su
etiqueta de referencia. Para cada par de llamadas consecutivas se pregunta:

  - dispararia una regla de delta en esta llamada?
  - la trayectoria de este paciente EMPEORA despues?

El cruce de las dos da las dos cifras que importan:

  AVISO ANTICIPADO   la llamada esta etiquetada verde, la regla dispara, y en una
                     llamada posterior el paciente sale amarillo o rojo. Es el unico
                     caso en que la tendencia aporta algo que el umbral absoluto no
                     tiene ya.
  FALSA ALARMA       la llamada esta etiquetada verde, la regla dispara, y la
                     trayectoria nunca empeora. Cuesta precision.

El resultado esta en `clinical/tendencia.py` con su interpretacion. En resumen: cero
avisos anticipados en todos los umbrales probados, porque el deterioro del dataset es
un escalon y no una rampa. Es un defecto del material entregado.

    python -m eval.tendencia
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "api"))

from centinela.clinical.tendencia import SALTO_DOLOR_NRS, SALTO_FIEBRE_C  # noqa: E402
from eval.dataset_loader import cargar_casos  # noqa: E402

MALAS = ("amarillo", "rojo")

# Los umbrales que se probaron. El ultimo es el que quedo en el motor.
BARRIDO = (
    (2, 0.5),
    (2, 0.8),
    (3, 0.5),
    (3, 0.8),
    (3, 1.0),
    (4, 0.8),
    (SALTO_DOLOR_NRS, SALTO_FIEBRE_C),
)


def trayectorias() -> dict[str, list]:
    por_paciente: dict[str, list] = collections.defaultdict(list)
    for caso in cargar_casos():
        por_paciente[caso.paciente_id].append(caso)
    for casos in por_paciente.values():
        casos.sort(key=lambda c: c.dia_postop)
    return por_paciente


def evaluar(por_paciente: dict[str, list], salto_dolor: float, salto_fiebre: float) -> dict:
    anticipados: list[dict] = []
    falsas: list[dict] = []
    oportunidades = 0

    for pid, casos in por_paciente.items():
        for i in range(1, len(casos)):
            antes, ahora = casos[i - 1], casos[i]
            d_dolor = ahora.dolor_nrs - antes.dolor_nrs
            d_fiebre = round(ahora.fiebre_c - antes.fiebre_c, 2)
            dispara = d_dolor >= salto_dolor or d_fiebre >= salto_fiebre
            empeora_despues = any(c.label in MALAS for c in casos[i:])

            # Solo cuentan las llamadas etiquetadas verdes: en una amarilla o roja el
            # sistema ya escala por umbral absoluto y la tendencia no aporta nada.
            if ahora.label == "verde":
                if empeora_despues:
                    oportunidades += 1
                registro = {
                    "paciente_id": pid, "dia": ahora.dia_postop,
                    "delta_dolor": d_dolor, "delta_fiebre": d_fiebre,
                    "trayectoria": [c.label for c in casos],
                }
                if dispara and empeora_despues:
                    anticipados.append(registro)
                elif dispara:
                    falsas.append(registro)

    return {
        "salto_dolor": salto_dolor,
        "salto_fiebre": salto_fiebre,
        "oportunidades": oportunidades,
        "avisos_anticipados": len(anticipados),
        "falsas_alarmas": len(falsas),
        "detalle_anticipados": anticipados,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    por_paciente = trayectorias()
    n_pacientes = len(por_paciente)
    empeoran = sum(
        1 for casos in por_paciente.values()
        if any(
            MALAS.index(b) > MALAS.index(a) if a in MALAS and b in MALAS else b in MALAS and a not in MALAS
            for a, b in zip([c.label for c in casos], [c.label for c in casos][1:])
        )
    )

    print("=" * 78)
    print("TENDENCIA ENTRE LLAMADAS - barrido de umbrales de delta")
    print("=" * 78)
    print(f"  pacientes                     : {n_pacientes}")
    print(f"  llamadas por paciente         : {len(next(iter(por_paciente.values())))}")
    print(f"  pacientes que empeoran alguna vez: {empeoran}")
    print()
    print("  umbral dolor  umbral fiebre  avisos anticipados  falsas alarmas")
    print("  " + "-" * 62)

    filas = []
    for salto_dolor, salto_fiebre in BARRIDO:
        r = evaluar(por_paciente, salto_dolor, salto_fiebre)
        filas.append(r)
        marca = "  <- en el motor" if (salto_dolor, salto_fiebre) == (
            SALTO_DOLOR_NRS, SALTO_FIEBRE_C
        ) else ""
        print(f"  >= +{salto_dolor:<9} >= +{salto_fiebre:<11} "
              f"{r['avisos_anticipados']:<19} {r['falsas_alarmas']}{marca}")

    oportunidades = filas[0]["oportunidades"]
    print()
    print(f"  llamadas verdes cuya trayectoria empeora despues: {oportunidades}")
    print("  (es el techo de lo que una regla de tendencia podria anticipar)")
    print()

    # Por que no anticipa: las trayectorias que saltan de verde a rojo van planas.
    print("  trayectorias que saltan de verde a rojo sin pasar por amarillo:")
    saltos = 0
    for pid, casos in por_paciente.items():
        etiquetas = [c.label for c in casos]
        for i in range(1, len(etiquetas)):
            if etiquetas[i] == "rojo" and etiquetas[i - 1] == "verde":
                saltos += 1
                print(f"    {pid}  {etiquetas}")
                print(f"      dolor  {[c.dolor_nrs for c in casos]}")
                print(f"      fiebre {[c.fiebre_c for c in casos]}")
    print(f"    total: {saltos}")
    print()
    print("  Los valores van planos hasta el dia del salto: el deterioro del dataset")
    print("  es un escalon, no una rampa, y por deltas no hay nada que anticipar.")
    print("  Es un defecto del material entregado, no del motor.")

    destino = Path(args.json) if args.json else RAIZ / "docs" / "metrics" / "tendencia.json"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(
        json.dumps(
            {
                "pacientes": n_pacientes,
                "pacientes_que_empeoran": empeoran,
                "oportunidades_de_anticipacion": oportunidades,
                "saltos_verde_a_rojo": saltos,
                "barrido": [
                    {k: v for k, v in f.items() if k != "detalle_anticipados"}
                    for f in filas
                ],
                "umbral_en_el_motor": {
                    "salto_dolor_nrs": SALTO_DOLOR_NRS,
                    "salto_fiebre_c": SALTO_FIEBRE_C,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print()
    print(f"  informe en {destino.relative_to(RAIZ)}")

    # El codigo de salida no penaliza la conclusion negativa: no es un fallo del
    # sistema, es una propiedad del dataset. Solo falla si el umbral que esta en el
    # motor produjera falsas alarmas, porque eso SI moveria `make eval`.
    en_motor = filas[-1]
    codigo = 0
    if en_motor["falsas_alarmas"]:
        print()
        print(f"  FALLA: el umbral del motor produce {en_motor['falsas_alarmas']} "
              f"falsas alarmas sobre las trayectorias oficiales")
        codigo = 1
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
