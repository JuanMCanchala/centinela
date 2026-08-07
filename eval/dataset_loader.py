"""Carga del dataset oficial del reto.

Une los cuatro .xlsx segun las reglas del README del reto:

    caso_id = "caso_" + trayectoria_id
    paciente_id une los cuatro archivos
    todos los libros tienen una sola hoja llamada `result`

Se usa solo en evaluacion offline. El agente en produccion nunca lee estos
archivos: recibe el cuadro clinico hablando con el paciente.
"""

from __future__ import annotations

import json
import os
import warnings
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

HOJA = "result"


def dataset_dir() -> Path:
    ruta = os.environ.get("CENTINELA_DATASET_DIR")
    if ruta:
        destino = Path(ruta)
    else:
        destino = Path(__file__).resolve().parents[2] / "ParticipantArtifacts" / "dataset"
    return destino


@dataclass(frozen=True)
class Caso:
    """Un caso = un paciente en un dia postoperatorio = una llamada."""

    caso_id: str
    paciente_id: str
    dia_postop: int
    label: str
    procedimiento: str
    modulo: str
    edad: int
    genero: str
    comorbilidades: list[str]
    nombre: str
    ciudad: str
    eps: str
    arquetipo: str
    # Cuadro clinico real (ground truth). El agente debe descubrirlo hablando.
    dolor_nrs: int
    fiebre_c: float
    movilidad: str
    herida: str
    apetito: str
    sueno: str


@dataclass(frozen=True)
class Turno:
    turno_idx: int
    hablante: str
    texto: str
    dialogo_id: str


@lru_cache(maxsize=1)
def _tablas() -> dict[str, pd.DataFrame]:
    base = dataset_dir()
    if not base.exists():
        raise FileNotFoundError(
            f"No encuentro el dataset en {base}. "
            "Clona TechSphere2026/ParticipantArtifacts al lado de este repo "
            "o exporta CENTINELA_DATASET_DIR."
        )
    tablas = {
        "conv": pd.read_excel(base / "dataset_final.xlsx", sheet_name=HOJA),
        "tray": pd.read_excel(base / "trayectorias_postop_silver.xlsx", sheet_name=HOJA),
        "clin": pd.read_excel(
            base / "perfiles_clinicos_pacientes_silver_contest.xlsx", sheet_name=HOJA
        ),
        "demo": pd.read_excel(base / "perfiles_pacientes_co.xlsx", sheet_name=HOJA),
    }
    return tablas


@lru_cache(maxsize=1)
def cargar_casos() -> list[Caso]:
    """Los 160 casos con su cuadro clinico y su etiqueta de referencia."""

    t = _tablas()
    conv, tray, clin, demo = t["conv"], t["tray"], t["clin"], t["demo"]

    labels = conv.groupby("caso_id")["label_ground_truth"].first()
    tray = tray.copy()
    tray["caso_id"] = "caso_" + tray["trayectoria_id"]

    perfil = clin.set_index("paciente_id")
    persona = demo.set_index("paciente_id")

    casos: list[Caso] = []
    for _, fila in tray.iterrows():
        pid = fila["paciente_id"]
        p = perfil.loc[pid]
        d = persona.loc[pid]
        casos.append(
            Caso(
                caso_id=fila["caso_id"],
                paciente_id=pid,
                dia_postop=int(fila["dia_postop"]),
                label=str(labels.get(fila["caso_id"], "")),
                procedimiento=str(p["procedimiento"]),
                modulo=str(p["modulo_synthea"]),
                edad=int(p["edad"]),
                genero=str(p["genero"]),
                comorbilidades=json.loads(p["comorbilidades"]),
                nombre=str(d["nombre_completo"]),
                ciudad=str(d["ciudad"]),
                eps=str(d["eps"]),
                arquetipo=str(fila["arquetipo_trayectoria"]),
                dolor_nrs=int(fila["dolor_nrs"]),
                fiebre_c=float(fila["fiebre_c"]),
                movilidad=str(fila["movilidad"]),
                herida=str(fila["herida"]),
                apetito=str(fila["apetito"]),
                sueno=str(fila["sueno"]),
            )
        )
    return casos


def cargar_conversacion(caso_id: str, capa: str) -> list[Turno]:
    """Los turnos de una llamada. `capa` es capa1_limpia o capa2_ruidosa."""

    conv = _tablas()["conv"]
    sel = conv[(conv["caso_id"] == caso_id) & (conv["capa"] == capa)].sort_values("turno_idx")
    turnos = [
        Turno(
            turno_idx=int(r["turno_idx"]),
            hablante=str(r["hablante"]),
            texto=str(r["texto"]),
            dialogo_id=str(r["dialogo_id"]),
        )
        for _, r in sel.iterrows()
    ]
    return turnos


def estilo_de(caso_id: str, capa: str) -> str:
    conv = _tablas()["conv"]
    sel = conv[(conv["caso_id"] == caso_id) & (conv["capa"] == capa)]
    estilo = str(sel["estilo_paciente"].iloc[0]) if len(sel) else ""
    return estilo
