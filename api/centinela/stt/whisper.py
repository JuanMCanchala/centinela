"""Transcripcion con faster-whisper (CTranslate2).

Por que local y no la API de Groq, teniendo Groq `whisper-large-v3-turbo` vivo y
siendo el STT libre segun las reglas del reto: la demo se evalua en vivo y una
dependencia de red en el camino critico de la conversacion es un punto de fallo
que no controlamos. Medido en esta maquina, `small` int8 en CPU transcribe un
turno tipico en decenas de milisegundos, asi que la nube no compraria latencia.

Queda como alternativa detras de una variable de entorno para el caso de una
maquina sin CPU suficiente (`CENTINELA_STT_BACKEND=groq`).

El VAD de Silero viene incluido en faster-whisper y se usa para dos cosas: filtrar
silencio antes de transcribir, y detectar el fin de habla del paciente, que es el
instante donde arranca la medicion de latencia que pide la rubrica.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


@dataclass
class Transcripcion:
    texto: str
    ms: float
    idioma: str = "es"
    probabilidad_idioma: float = 0.0
    duracion_audio_s: float = 0.0
    segmentos: list[dict] = field(default_factory=list)
    sin_habla: bool = False

    @property
    def factor_tiempo_real(self) -> float:
        return (self.ms / 1000) / self.duracion_audio_s if self.duracion_audio_s > 0 else 0.0


class WhisperSTT:
    def __init__(
        self,
        tamano: str = "small",
        dispositivo: str = "cpu",
        tipo_computo: str = "int8",
        dir_modelos: Path | None = None,
    ) -> None:
        self.tamano = tamano
        self.dispositivo = dispositivo
        self.tipo_computo = tipo_computo
        self.dir_modelos = dir_modelos
        self._modelo = None

    @property
    def modelo(self):
        if self._modelo is None:
            from faster_whisper import WhisperModel

            kwargs = {
                "device": self.dispositivo,
                "compute_type": self.tipo_computo,
            }
            if self.dir_modelos:
                kwargs["download_root"] = str(self.dir_modelos)
            self._modelo = WhisperModel(self.tamano, **kwargs)
        return self._modelo

    def estado(self) -> dict:
        return {
            "motor": "faster-whisper",
            "modelo": self.tamano,
            "dispositivo": f"{self.dispositivo}/{self.tipo_computo}",
            "cargado": self._modelo is not None,
        }

    # ------------------------------------------------------------------

    def transcribir(self, muestras: np.ndarray, frecuencia: int = 16000) -> Transcripcion:
        """Transcribe audio mono float32 en [-1, 1]."""

        t0 = time.perf_counter()
        duracion = len(muestras) / frecuencia

        segmentos, info = self.modelo.transcribe(
            muestras,
            language="es",
            beam_size=1,               # greedy: el habla es corta y la latencia manda
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 400},
            condition_on_previous_text=False,  # evita arrastrar alucinaciones entre turnos
            temperature=0.0,
        )

        piezas = []
        detalle = []
        for s in segmentos:
            piezas.append(s.text)
            detalle.append({
                "inicio": round(s.start, 2),
                "fin": round(s.end, 2),
                "texto": s.text.strip(),
                "prob_sin_habla": round(getattr(s, "no_speech_prob", 0.0), 3),
            })

        texto = " ".join(p.strip() for p in piezas).strip()
        ms = (time.perf_counter() - t0) * 1000

        return Transcripcion(
            texto=texto,
            ms=round(ms, 2),
            idioma=getattr(info, "language", "es"),
            probabilidad_idioma=round(getattr(info, "language_probability", 0.0), 3),
            duracion_audio_s=round(duracion, 2),
            segmentos=detalle,
            sin_habla=not texto,
        )

    def calentar(self) -> None:
        """Primera inferencia fuera del camino critico."""

        silencio = np.zeros(16000, dtype=np.float32)
        self.transcribir(silencio)


# --------------------------------------------------------------------------
# Utilidades de audio para el WebSocket
# --------------------------------------------------------------------------

def pcm16_a_float32(datos: bytes) -> np.ndarray:
    """PCM 16 bits little-endian -> float32 en [-1, 1]."""

    enteros = np.frombuffer(datos, dtype="<i2")
    return (enteros.astype(np.float32) / 32768.0).copy()


def energia_rms(muestras: np.ndarray) -> float:
    valor = float(np.sqrt(np.mean(np.square(muestras)))) if len(muestras) else 0.0
    return valor
