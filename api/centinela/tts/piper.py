"""Sintesis de voz con Piper, y el cache que la saca del camino critico.

La rubrica mide la latencia "desde que el paciente termina de hablar hasta que
empieza a sonar el audio del agente". El TTS esta justo en el medio de esa
medicion, asi que la estrategia es no ejecutarlo cuando se puede evitar.

Se puede evitar casi siempre. La conversacion la conduce una maquina de estados
sobre un guion de seis dominios, asi que las ~35 locuciones que el agente puede
decir se conocen antes de que suene el telefono. `pre_renderizar()` las sintetiza
una vez y las deja en disco. En un turno de guion, "sintetizar" es leer un
archivo: microsegundos en lugar de cientos de milisegundos.

El TTS en caliente queda solo para lo que no se puede prever: la respuesta a una
pregunta clinica del paciente y las frases que llevan su nombre. Y ahi se
sintetiza **por frase**, no por respuesta completa, para que el audio empiece a
sonar mientras el modelo todavia esta generando la segunda frase.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import struct
import subprocess
import time
import wave
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[3]
DIR_PIPER = RAIZ / "data" / "piper"
DIR_CACHE = RAIZ / "data" / "audio_cache"

FRECUENCIA_OBJETIVO = 22050

# Orden de preferencia de voz, decidido con `scripts/bench_voces.py` sobre esta
# maquina. La columna que manda es el factor de tiempo real (RTF):
#
#   es_MX-ald-medium        RTF 0.352   60 MB   <- elegida
#   es_ES-carlfm-x_low      RTF 0.334   27 MB
#   es_ES-sharvard-medium   RTF 0.345   73 MB
#   es_ES-davefx-medium     RTF 0.350   60 MB
#   es_MX-claude-high       RTF 0.403   60 MB
#   es_AR-daniela-high      RTF 0.983  109 MB
#
# Las variantes `high` estan descartadas: a RTF ~1.0 generar la respuesta cuesta
# lo mismo que dura, y el paciente espera todo ese tiempo. Entre las rapidas la
# diferencia es del 5%, asi que decide el acento: `es_MX` es la unica
# latinoamericana del grupo rapido y el paciente del reto es colombiano.
PREFERENCIA_VOCES = (
    "es_MX-ald-medium",
    "es_ES-sharvard-medium",
    "es_ES-davefx-medium",
    "es_ES-carlfm-x_low",
    "es_MX-claude-high",
    "es_AR-daniela-high",
)


@dataclass
class AudioSintetizado:
    wav: bytes
    ms_sintesis: float
    desde_cache: bool
    clave: str | None = None
    frecuencia: int = FRECUENCIA_OBJETIVO

    @property
    def duracion_s(self) -> float:
        # 44 bytes de cabecera WAV, 16 bits mono
        muestras = max(0, len(self.wav) - 44) / 2
        return muestras / self.frecuencia


class PiperTTS:
    def __init__(
        self,
        binario: Path | None = None,
        voz: Path | None = None,
        dir_cache: Path | None = None,
    ) -> None:
        self.binario = binario or self._localizar_binario()
        self.voz = voz or self._localizar_voz()
        self.dir_cache = Path(dir_cache or DIR_CACHE)
        self.dir_cache.mkdir(parents=True, exist_ok=True)
        self._memoria: dict[str, bytes] = {}
        # Proceso residente: el binario carga el modelo ONNX una sola vez y
        # despues atiende peticiones por stdin. Medido en esta maquina, arrancar
        # un proceso por frase costaba ~540 ms extra por sintesis.
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._tmp = self.dir_cache / "_tmp"
        self._tmp.mkdir(parents=True, exist_ok=True)
        self._contador = 0

    # ------------------------------------------------------------------

    @property
    def disponible(self) -> bool:
        return (
            self.binario is not None
            and self.binario.exists()
            and self.voz is not None
            and self.voz.exists()
        )

    def estado(self) -> dict:
        return {
            "motor": "piper",
            "disponible": self.disponible,
            "binario": str(self.binario) if self.binario else None,
            "voz": self.voz.stem if self.voz else None,
            "locuciones_en_cache": len(list(self.dir_cache.glob("*.wav"))),
            "respaldo": "SpeechSynthesis del navegador si el motor no esta disponible",
        }

    @staticmethod
    def _localizar_binario() -> Path | None:
        sufijo = ".exe" if platform.system() == "Windows" else ""
        desde_env = os.environ.get("CENTINELA_PIPER_BIN")
        if desde_env:
            candidato = Path(desde_env)
            encontrado = candidato if candidato.exists() else None
        else:
            candidatos = list(DIR_PIPER.rglob(f"piper{sufijo}"))
            archivos = [c for c in candidatos if c.is_file()]
            encontrado = archivos[0] if archivos else None
        return encontrado

    @staticmethod
    def _localizar_voz() -> Path | None:
        desde_env = os.environ.get("CENTINELA_PIPER_VOZ")
        if desde_env:
            candidato = Path(desde_env)
            encontrado = candidato if candidato.exists() else None
        else:
            dir_voces = DIR_PIPER / "voces"
            encontrado = None
            for nombre in PREFERENCIA_VOCES:
                candidato = dir_voces / f"{nombre}.onnx"
                if candidato.exists() and encontrado is None:
                    encontrado = candidato
            if encontrado is None:
                # Ninguna de las conocidas: se toma cualquiera que haya.
                voces = sorted(dir_voces.glob("*.onnx"))
                encontrado = voces[0] if voces else None
        return encontrado

    # ------------------------------------------------------------------

    async def sintetizar(self, texto: str, clave: str | None = None) -> AudioSintetizado:
        """Devuelve WAV. Si `clave` esta en cache, no ejecuta el motor."""

        t0 = time.perf_counter()

        if clave and clave in self._memoria:
            return AudioSintetizado(
                wav=self._memoria[clave],
                ms_sintesis=(time.perf_counter() - t0) * 1000,
                desde_cache=True,
                clave=clave,
            )

        if clave:
            archivo = self.dir_cache / f"{clave}.wav"
            if archivo.exists():
                datos = archivo.read_bytes()
                self._memoria[clave] = datos
                return AudioSintetizado(
                    wav=datos,
                    ms_sintesis=(time.perf_counter() - t0) * 1000,
                    desde_cache=True,
                    clave=clave,
                )

        datos = await self._ejecutar_piper(texto)
        ms = (time.perf_counter() - t0) * 1000

        if clave and datos:
            (self.dir_cache / f"{clave}.wav").write_bytes(datos)
            self._memoria[clave] = datos

        return AudioSintetizado(wav=datos, ms_sintesis=ms, desde_cache=False, clave=clave)

    async def _asegurar_proceso(self) -> bool:
        """Arranca el proceso residente de Piper si no esta vivo."""

        vivo = self._proc is not None and self._proc.returncode is None

        if not vivo and self.disponible:
            self._proc = await asyncio.create_subprocess_exec(
                str(self.binario),
                "--model", str(self.voz),
                "--json-input",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=str(self.binario.parent),
            )
            vivo = True

        return vivo

    async def _ejecutar_piper(self, texto: str) -> bytes:
        if not self.disponible:
            return b""

        # Un solo proceso atiende una peticion a la vez: el protocolo de
        # --json-input es una linea de entrada por una linea de salida, asi que
        # dos corrutinas escribiendo a la vez se leerian la respuesta cruzada.
        async with self._lock:
            listo = await self._asegurar_proceso()
            datos = b""

            if listo:
                self._contador += 1
                salida = self._tmp / f"t{self._contador:06d}.wav"
                peticion = json.dumps(
                    {"text": texto, "output_file": str(salida)}, ensure_ascii=False
                )
                try:
                    self._proc.stdin.write((peticion + "\n").encode("utf-8"))
                    await self._proc.stdin.drain()
                    await self._proc.stdout.readline()  # confirmacion de piper
                except (BrokenPipeError, ConnectionResetError, AttributeError):
                    # El proceso murio: se reintenta una vez con uno nuevo.
                    self._proc = None
                    listo = await self._asegurar_proceso()
                    if listo:
                        self._proc.stdin.write((peticion + "\n").encode("utf-8"))
                        await self._proc.stdin.drain()
                        await self._proc.stdout.readline()

                if salida.exists():
                    datos = salida.read_bytes()
                    salida.unlink(missing_ok=True)

        return datos

    async def cerrar(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            self._proc.terminate()
            await self._proc.wait()
        self._proc = None

    # ------------------------------------------------------------------

    async def sintetizar_por_frases(self, texto: str):
        """Cede audio frase por frase, en el orden en que se debe reproducir.

        Es la mitad del presupuesto de latencia en los turnos de texto libre: el
        paciente oye la primera frase mientras la segunda todavia no existe.
        """

        for frase in partir_en_frases(texto):
            if frase.strip():
                audio = await self.sintetizar(frase)
                yield frase, audio

    # ------------------------------------------------------------------

    async def pre_renderizar(self, locuciones, forzar: bool = False) -> dict:
        """Sintetiza el guion completo. Se corre una vez al arrancar."""

        generadas = 0
        reutilizadas = 0
        fallidas: list[str] = []
        t0 = time.perf_counter()

        for loc in locuciones:
            archivo = self.dir_cache / f"{loc.clave}.wav"
            if archivo.exists() and not forzar:
                reutilizadas += 1
                self._memoria[loc.clave] = archivo.read_bytes()
            else:
                # Las locuciones con marcador de formato ({nombre}) no se
                # pre-renderizan: su texto depende del paciente.
                if "{" in loc.texto:
                    continue
                audio = await self.sintetizar(loc.texto, clave=loc.clave)
                if audio.wav:
                    generadas += 1
                else:
                    fallidas.append(loc.clave)

        return {
            "generadas": generadas,
            "reutilizadas": reutilizadas,
            "fallidas": fallidas,
            "segundos": round(time.perf_counter() - t0, 2),
            "total_en_cache": len(list(self.dir_cache.glob("*.wav"))),
        }


# --------------------------------------------------------------------------

RE_FRASE = re.compile(r"(?<=[.!?])\s+")


def partir_en_frases(texto: str) -> list[str]:
    """Corta en frases pronunciables.

    Une los fragmentos demasiado cortos: sintetizar "Si." como una unidad
    produce un audio abrupto y suma una invocacion del motor sin ganar nada.
    """

    crudos = [f.strip() for f in RE_FRASE.split(texto.strip()) if f.strip()]
    frases: list[str] = []

    for frase in crudos:
        if frases and len(frase) < 25:
            frases[-1] = f"{frases[-1]} {frase}"
        else:
            frases.append(frase)

    return frases


def concatenar_wav(trozos: list[bytes]) -> bytes:
    """Une varios WAV en uno. Los fragmentos de un turno se envian juntos."""

    utiles = [t for t in trozos if t and len(t) > 44]

    if not utiles:
        salida = b""
    elif len(utiles) == 1:
        salida = utiles[0]
    else:
        marcos: list[bytes] = []
        parametros = None
        for t in utiles:
            import io

            with wave.open(io.BytesIO(t), "rb") as w:
                if parametros is None:
                    parametros = w.getparams()
                marcos.append(w.readframes(w.getnframes()))

        import io

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as w:
            w.setnchannels(parametros.nchannels)
            w.setsampwidth(parametros.sampwidth)
            w.setframerate(parametros.framerate)
            w.writeframes(b"".join(marcos))
        salida = buffer.getvalue()

    return salida
