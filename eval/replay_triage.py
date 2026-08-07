"""Arnes de evaluacion del motor de decision sobre los 160 casos oficiales.

Mide lo unico que de verdad importa en salud: cuantas veces el sistema NO
alerto cuando habia que alertar.

Esto no es un test de juguete: es la fuente de la tabla de metricas del README.
El jurado puede volver a correrlo (`make eval`) y obtener exactamente los mismos
numeros, porque el motor de decision no tiene ninguna fuente de aleatoriedad.

    python -m eval.replay_triage
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))
sys.path.insert(0, str(RAIZ))

from centinela.clinical.triage_engine import TriageEngine  # noqa: E402
from centinela.models import ClinicalState, Observacion, Procedencia  # noqa: E402
from eval.dataset_loader import Caso, cargar_casos  # noqa: E402

NIVELES = ("verde", "amarillo", "rojo")
ORDEN = {"verde": 0, "amarillo": 1, "rojo": 2}


def estado_desde_ground_truth(caso: Caso) -> ClinicalState:
    """Construye el estado clinico con los seis campos ya conocidos.

    Aisla el motor de decision del error de extraccion: si aqui aparece un
    falso negativo, es culpa de las reglas y de nadie mas.
    """

    def obs(valor: float | str) -> Observacion:
        return Observacion(
            valor=valor,
            conocido=True,
            procedencia=Procedencia(turno_idx=-1, cita_paciente="<ground truth>", inferido=False),
        )

    estado = ClinicalState(
        dolor_nrs=obs(caso.dolor_nrs),
        fiebre_c=obs(caso.fiebre_c),
        movilidad=obs(caso.movilidad),
        herida=obs(caso.herida),
        apetito=obs(caso.apetito),
        sueno=obs(caso.sueno),
    )
    return estado


def matriz(reales: list[str], predichos: list[str]) -> dict[str, dict[str, int]]:
    m = {r: {p: 0 for p in NIVELES} for r in NIVELES}
    for r, p in zip(reales, predichos):
        m[r][p] += 1
    return m


def render_matriz(m: dict[str, dict[str, int]]) -> str:
    ancho = 11
    cabecera = "real \\ pred".ljust(14) + "".join(n.ljust(ancho) for n in NIVELES)
    filas = [cabecera, "-" * len(cabecera)]
    for r in NIVELES:
        filas.append(r.ljust(14) + "".join(str(m[r][p]).ljust(ancho) for p in NIVELES))
    return "\n".join(filas)


def metricas_por_clase(m: dict[str, dict[str, int]]) -> dict[str, dict[str, float]]:
    salida: dict[str, dict[str, float]] = {}
    for clase in NIVELES:
        tp = m[clase][clase]
        real = sum(m[clase].values())
        pred = sum(m[r][clase] for r in NIVELES)
        salida[clase] = {
            "recall": round(tp / real, 4) if real else 0.0,
            "precision": round(tp / pred, 4) if pred else 0.0,
            "n_real": real,
            "n_predicho": pred,
        }
    return salida


def main() -> int:
    casos = cargar_casos()
    motor = TriageEngine()

    reales: list[str] = []
    predichos: list[str] = []
    falsos_negativos: list[dict] = []
    subescalados: list[dict] = []

    for caso in casos:
        estado = estado_desde_ground_truth(caso)
        decision = motor.evaluar(estado, estilo_paciente=None, cerrar=True)
        real, pred = caso.label, decision.nivel.value
        reales.append(real)
        predichos.append(pred)

        # Falso negativo clinico: se necesitaba escalar y se cerro en verde,
        # o era rojo y se trato como amarillo.
        if ORDEN[pred] < ORDEN[real]:
            registro = {
                "caso_id": caso.caso_id,
                "real": real,
                "predicho": pred,
                "procedimiento": caso.procedimiento,
                "dia_postop": caso.dia_postop,
                "dolor_nrs": caso.dolor_nrs,
                "fiebre_c": caso.fiebre_c,
                "movilidad": caso.movilidad,
                "herida": caso.herida,
                "apetito": caso.apetito,
                "sueno": caso.sueno,
                "motivo_motor": decision.motivo,
            }
            if pred == "verde":
                falsos_negativos.append(registro)
            else:
                subescalados.append(registro)

    m = matriz(reales, predichos)
    por_clase = metricas_por_clase(m)
    sobre_escalados = sum(
        m[r][p] for r in NIVELES for p in NIVELES if ORDEN[p] > ORDEN[r]
    )
    exactos = sum(m[n][n] for n in NIVELES)

    print("=" * 78)
    print("CENTINELA - motor de decision sobre los 160 casos oficiales del reto")
    print(f"version de reglas: {motor.config.version}")
    print("=" * 78)
    print()
    print(render_matriz(m))
    print()
    for clase in NIVELES:
        c = por_clase[clase]
        print(
            f"  {clase:9s} recall={c['recall']:.3f}  precision={c['precision']:.3f}"
            f"  (real={c['n_real']}, predicho={c['n_predicho']})"
        )
    print()
    print(f"  exactitud global            : {exactos}/{len(casos)} = {exactos/len(casos):.3f}")
    print(f"  FALSOS NEGATIVOS CLINICOS  : {len(falsos_negativos)}   <-- la metrica que manda")
    print(f"  rojos sub-escalados        : {len(subescalados)}")
    print(f"  sobre-escalamientos        : {sobre_escalados} "
          f"({sobre_escalados/len(casos):.1%}) - direccion segura")
    print()

    if falsos_negativos:
        print("!! FALSOS NEGATIVOS - hay que corregir las reglas antes de seguir:")
        for f in falsos_negativos:
            print(f"   {f['caso_id']} real={f['real']} pred={f['predicho']} "
                  f"dolor={f['dolor_nrs']} fiebre={f['fiebre_c']} herida={f['herida']}")
        print()

    distrib = Counter(reales)
    resultado = {
        "version_reglas": motor.config.version,
        "n_casos": len(casos),
        "distribucion_real": dict(distrib),
        "matriz_confusion": m,
        "por_clase": por_clase,
        "exactitud_global": round(exactos / len(casos), 4),
        "falsos_negativos_clinicos": len(falsos_negativos),
        "rojos_subescalados": len(subescalados),
        "sobre_escalamientos": sobre_escalados,
        "detalle_falsos_negativos": falsos_negativos,
        "detalle_subescalados": subescalados,
    }

    salida = RAIZ / "docs" / "metrics" / "triage_160_casos.json"
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text(json.dumps(resultado, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"metricas escritas en {salida.relative_to(RAIZ)}")

    codigo = 1 if falsos_negativos or subescalados else 0
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
