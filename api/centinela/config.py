"""Configuracion por variables de entorno, con valores por defecto que funcionan.

Criterio: `docker compose up` sin tocar nada debe levantar un sistema completo y
usable. Cada variable existe para que el jurado pueda cambiar una decision sin
editar codigo, no para obligarlo a configurar nada.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]


def _ruta(env: str, defecto: Path) -> Path:
    valor = os.environ.get(env)
    return Path(valor) if valor else defecto


@dataclass
class Config:
    # --- datos ---
    dir_index: Path = field(default_factory=lambda: _ruta(
        "CENTINELA_DIR_INDEX", RAIZ / "data" / "index"))
    dir_runtime: Path = field(default_factory=lambda: _ruta(
        "CENTINELA_DIR_RUNTIME", RAIZ / "data" / "runtime"))
    dir_audio_cache: Path = field(default_factory=lambda: _ruta(
        "CENTINELA_DIR_AUDIO", RAIZ / "data" / "audio_cache"))
    dir_subidas: Path = field(default_factory=lambda: _ruta(
        "CENTINELA_DIR_SUBIDAS", RAIZ / "data" / "subidas"))

    # --- modelo de lenguaje (compuerta G3 del reto) ---
    modelo_llm: str = os.environ.get(
        "CENTINELA_LLM_MODEL", "phi3.5:3.8b-mini-instruct-q4_K_M")
    url_ollama: str = os.environ.get("CENTINELA_OLLAMA_URL", "http://127.0.0.1:11434")

    # --- voz ---
    #
    # Vacio a proposito: `WhisperSTT` detecta si hay GPU y elige el modelo en
    # consecuencia (`large-v3-turbo` en CUDA, `small` en CPU). Fijar "small" aqui
    # como estaba antes impedia usar la GPU aunque estuviera disponible, y con
    # `small` el sistema transcribia "si, soy yo" como "Season young".
    #
    # Las tres variables de entorno siguen mandando si se quiere forzar algo:
    #   CENTINELA_STT_MODEL=medium CENTINELA_STT_DEVICE=cpu
    modelo_stt: str = os.environ.get("CENTINELA_STT_MODEL", "")
    dispositivo_stt: str = os.environ.get("CENTINELA_STT_DEVICE", "")
    tipo_computo_stt: str = os.environ.get("CENTINELA_STT_COMPUTE", "")

    # --- comportamiento ---
    calentar_al_arrancar: bool = os.environ.get("CENTINELA_WARMUP", "1") == "1"
    pre_renderizar_audio: bool = os.environ.get("CENTINELA_PRERENDER", "1") == "1"

    @property
    def ruta_metricas(self) -> Path:
        return self.dir_runtime / "metricas.jsonl"

    def asegurar_directorios(self) -> None:
        for d in (self.dir_index, self.dir_runtime, self.dir_audio_cache, self.dir_subidas):
            d.mkdir(parents=True, exist_ok=True)

    def a_dict(self) -> dict:
        return {
            "modelo_llm": self.modelo_llm,
            "url_ollama": self.url_ollama,
            "stt_forzado": {
                "modelo": self.modelo_stt or "(autodetectado)",
                "dispositivo": self.dispositivo_stt or "(autodetectado)",
                "computo": self.tipo_computo_stt or "(autodetectado)",
            },
            "dir_index": str(self.dir_index),
            "dir_runtime": str(self.dir_runtime),
        }


config = Config()
