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
import io
import json
import os
import platform
import re
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from ..obs.log import log

import numpy as np

from .hablado import para_voz

RAIZ = Path(__file__).resolve().parents[3]
DIR_PIPER = RAIZ / "data" / "piper"
DIR_CACHE = RAIZ / "data" / "audio_cache"

FRECUENCIA_OBJETIVO = 22050

# --------------------------------------------------------------------------
# Prosodia y tratamiento del audio
#
# Los knobs de prosodia de Piper NO son por peticion: comprobado que en modo
# `--json-input` se ignoran (la misma frase con length_scale 1.25, noise_w 0.6 y
# sentence_silence 0.5 dio 3.044 s contra 3.033 s sin ellos). Solo funcionan como
# flags al arrancar el proceso, asi que hay un ajuste global y un perfil aparte para
# lo que se sintetiza una vez al pre-renderizar.
# --------------------------------------------------------------------------

# Queda en 1.0, y el porque es una medicion: este modelo tiene ruido de duracion de
# fonema (`noise_w` 0.8 por defecto), asi que dos sintesis del MISMO texto con el MISMO
# length_scale ya difieren un 3 % entre si. Medido con 4 repeticiones:
#
#   length_scale 1.0   ->  5.117 s   (spread propio 3.1 %)
#   length_scale 1.05  ->  5.083 s   <- indistinguible del anterior
#   length_scale 1.5   ->  6.853 s   (+34 %, aqui si se oye)
#
# Un ajuste "algo mas pausado" del 5 % no existe en la practica: se pierde dentro de la
# varianza del modelo. Antes que dejar un parametro que aparenta hacer algo, se deja en
# 1.0 y se expone la variable para quien quiera moverlo a un valor que si se distinga.
# `scripts/muestras_prosodia.py` genera el A/B para decidirlo por oido.
LENGTH_SCALE = float(os.environ.get("CENTINELA_TTS_VELOCIDAD", "1.0"))

# La instruccion de urgencias se dice mas despacio y con menos variabilidad de fonema.
# Es la unica frase de la llamada cuya perdida es daño clinico, y esta fija en el guion:
# se sintetiza una vez al arrancar, nunca en caliente, asi que no toca el camino critico
# de latencia. El valor esta por encima del ruido de duracion medido arriba -- a 1.16 se
# quedaba en el limite, y un enfasis que no se distingue del azar no es un enfasis.
LENGTH_SCALE_ENFASIS = 1.22
NOISE_W_ENFASIS = 0.55

# Umbral de "aqui no hay voz", en amplitud normalizada. Por debajo de esto Piper
# emite ruido de fondo del modelo, no habla.
UMBRAL_SILENCIO = 0.008

# Lo que se deja de silencio al recortar. No es cero: cortar exactamente en la
# primera muestra audible produce un chasquido al arrancar la reproduccion.
DEJAR_MS_INICIAL = 15
DEJAR_MS_FINAL = 55

# Pausa entre los fragmentos de un turno (acuse + pregunta, por ejemplo). Constante
# a proposito: lo que delataba a la maquina era que la cola de silencio de Piper
# medida sobre las 59 locuciones iba de 200 a 785 ms, asi que el hueco entre dos
# frases del mismo turno cambiaba de duracion sin motivo.
PAUSA_ENTRE_FRAGMENTOS_MS = 90

# Nivel objetivo. Piper entrega todo a pico 1.0 (las 59 locuciones del guion, sin
# excepcion) mientras el RMS de voz varia 4.2 dB entre ellas: cero headroom y saltos
# de volumen percibido entre frases. Se iguala el RMS y se deja el pico bajo el techo.
RMS_OBJETIVO = 0.16
PICO_MAXIMO = 0.891  # -1 dBFS

# El techo se mide en un percentil y no en el maximo absoluto, y la razon es medible:
# las 59 locuciones tienen pico exactamente 1.000, asi que usar el maximo hacia que
# TODAS recibieran la misma reduccion y el RMS siguiera variando los mismos 4.1 dB --
# la normalizacion no normalizaba nada. Con el percentil 99.9 el spread baja a 0.0 dB
# a cambio de recortar como mucho el 0.1 % de las muestras, que son transitorios de
# consonante y no se oyen. El factor de cresta de esta voz va de 3.6 a 5.8.
PERCENTIL_PICO = 99.9

# Cambiar cualquiera de los parametros de arriba invalida el audio ya cacheado, igual
# que cambiar el texto. Subir este numero es lo que fuerza el re-renderizado.
VERSION_TRATAMIENTO = 4

MANIFIESTO = "manifiesto.json"

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
        # Firma del texto con el que se genero cada WAV. Se carga tarde porque el
        # constructor corre en el arranque de la API y esto es un archivo en disco.
        self._firmas: dict | None = None
        self._firmas_sucias = False

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
        """Devuelve WAV. Si `clave` esta en cache y el texto no cambio, no ejecuta el motor."""

        t0 = time.perf_counter()
        cacheable = bool(clave) and self._vigente(clave, texto)

        if cacheable and clave in self._memoria:
            return AudioSintetizado(
                wav=self._memoria[clave],
                ms_sintesis=(time.perf_counter() - t0) * 1000,
                desde_cache=True,
                clave=clave,
            )

        if cacheable:
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

        datos = tratar(await self._ejecutar_piper(para_voz(texto)))
        ms = (time.perf_counter() - t0) * 1000

        # Una sintesis muda NO entra al cache. Si entrara, su firma quedaria correcta
        # -- el texto no cambio -- y no se re-sintetizaria nunca: la locucion quedaria
        # muda para siempre. Ver `tiene_voz`.
        if clave and datos and not tiene_voz(datos):
            log("sintesis_muda_no_cacheada", nivel="error", clave=clave,
                bytes_devueltos=len(datos), texto=texto[:60])
        elif clave and datos:
            (self.dir_cache / f"{clave}.wav").write_bytes(datos)
            self._memoria[clave] = datos
            self._anotar(clave, texto)

        return AudioSintetizado(wav=datos, ms_sintesis=ms, desde_cache=False, clave=clave)

    # ------------------------------------------------------------------
    # El cache y su invalidacion
    #
    # La clave nombra el archivo, no su contenido, asi que hasta aqui editar una
    # locucion no cambiaba su audio: el WAV viejo seguia en disco y se servia igual.
    # Es el defecto que dejaba la correccion ortografica del guion sin efecto audible.
    # El manifiesto guarda de que texto y con que tratamiento salio cada WAV.
    # ------------------------------------------------------------------

    def _firma(self, texto: str, enfasis: bool = False) -> str:
        crudo = f"{VERSION_TRATAMIENTO}|{LENGTH_SCALE}|{int(enfasis)}|{para_voz(texto)}"
        return hashlib.sha256(crudo.encode("utf-8")).hexdigest()[:16]

    @property
    def _manifiesto(self) -> dict:
        if self._firmas is None:
            archivo = self.dir_cache / MANIFIESTO
            if archivo.exists():
                try:
                    self._firmas = json.loads(archivo.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    # Un manifiesto ilegible se trata como cache vacio: se re-sintetiza
                    # todo, que es lento pero correcto. Lo contrario seria servir audio
                    # que no corresponde al texto.
                    self._firmas = {}
            else:
                self._firmas = {}
        return self._firmas

    def _vigente(self, clave: str, texto: str, enfasis: bool = False) -> bool:
        return self._manifiesto.get(clave) == self._firma(texto, enfasis)

    def _anotar(self, clave: str, texto: str, enfasis: bool = False) -> None:
        self._manifiesto[clave] = self._firma(texto, enfasis)
        self._firmas_sucias = True

    def _guardar_manifiesto(self) -> None:
        if self._firmas_sucias:
            (self.dir_cache / MANIFIESTO).write_text(
                json.dumps(self._manifiesto, indent=2, sort_keys=True), encoding="utf-8"
            )
            self._firmas_sucias = False

    async def _asegurar_proceso(self) -> bool:
        """Arranca el proceso residente de Piper si no esta vivo."""

        vivo = self._proc is not None and self._proc.returncode is None

        if not vivo and self.disponible:
            self._proc = await asyncio.create_subprocess_exec(
                str(self.binario),
                "--model", str(self.voz),
                "--length_scale", str(LENGTH_SCALE),
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

    async def _sintetizar_con_enfasis(self, texto: str) -> bytes:
        """Una invocacion aparte del binario, mas lenta y mas nitida.

        Los knobs de prosodia solo existen como flags de arranque, asi que esto no
        puede salir del proceso residente. No importa: las locuciones con enfasis son
        las tres de la instruccion de urgencias, fijas en el guion, y se sintetizan una
        sola vez al pre-renderizar. Nunca caen en el camino critico de latencia.
        """

        datos = b""

        if self.disponible:
            self._contador += 1
            salida = self._tmp / f"e{self._contador:06d}.wav"
            proc = await asyncio.create_subprocess_exec(
                str(self.binario),
                "--model", str(self.voz),
                "--length_scale", str(LENGTH_SCALE_ENFASIS),
                "--noise_w", str(NOISE_W_ENFASIS),
                "--output_file", str(salida),
                "--quiet",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=str(self.binario.parent),
            )
            await proc.communicate(para_voz(texto).encode("utf-8") + b"\n")
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
        renovadas = 0
        con_enfasis = 0
        fallidas: list[str] = []
        t0 = time.perf_counter()

        for loc in locuciones:
            archivo = self.dir_cache / f"{loc.clave}.wav"
            enfasis = getattr(loc, "enfasis", False)
            # Las locuciones con marcador de formato ({nombre}) no se pre-renderizan:
            # su texto depende del paciente.
            plantilla = "{" in loc.texto
            vigente = self._vigente(loc.clave, loc.texto, enfasis)

            if plantilla:
                pass
            elif archivo.exists() and vigente and not forzar:
                reutilizadas += 1
                self._memoria[loc.clave] = archivo.read_bytes()
            else:
                # Se distingue lo nuevo de lo que se rehace porque cambio el texto: es
                # la señal de que la correccion del guion llego de verdad al audio.
                if archivo.exists():
                    renovadas += 1

                if enfasis:
                    datos = tratar(await self._sintetizar_con_enfasis(loc.texto))
                    if datos and not tiene_voz(datos):
                        log("sintesis_muda_no_cacheada", nivel="error",
                            clave=loc.clave, con_enfasis=True,
                            bytes_devueltos=len(datos))
                        datos = b""
                    elif datos:
                        archivo.write_bytes(datos)
                        self._memoria[loc.clave] = datos
                        self._anotar(loc.clave, loc.texto, enfasis)
                        con_enfasis += 1
                else:
                    datos = (await self.sintetizar(loc.texto, clave=loc.clave)).wav

                if datos:
                    generadas += 1
                else:
                    fallidas.append(loc.clave)

        self._guardar_manifiesto()

        return {
            "generadas": generadas,
            "reutilizadas": reutilizadas,
            "renovadas_por_cambio_de_texto": renovadas,
            "con_enfasis": con_enfasis,
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


# --------------------------------------------------------------------------
# Tratamiento del audio
#
# Piper entrega la sintesis cruda: con la cola de silencio que le salga al modelo y
# escalada a pico completo. Las dos cosas se oyen. Estas funciones son puras y estan
# medidas en `tests/test_audio_hablado.py`.
# --------------------------------------------------------------------------


def _muestras(wav: bytes) -> tuple[np.ndarray, int]:
    """Las muestras como enteros de 16 bits, y su frecuencia."""

    if not wav or len(wav) <= 44:
        datos, frecuencia = np.zeros(0, dtype=np.int16), FRECUENCIA_OBJETIVO
    else:
        with wave.open(io.BytesIO(wav), "rb") as w:
            frecuencia = w.getframerate()
            datos = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return datos, frecuencia


def _envolver(datos: np.ndarray, frecuencia: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(frecuencia)
        w.writeframes(datos.astype(np.int16).tobytes())
    return buffer.getvalue()


def recortar_silencio(
    wav: bytes,
    dejar_inicial_ms: int = DEJAR_MS_INICIAL,
    dejar_final_ms: int = DEJAR_MS_FINAL,
) -> bytes:
    """Deja el silencio de los extremos en una duracion conocida.

    Existe porque la cola que Piper deja al final de cada sintesis medía entre 200 y
    785 ms segun la locucion. Concatenando dos fragmentos de un mismo turno, ese hueco
    variable caia justo en medio de lo que deberia sonar como una sola intervencion.
    """

    datos, frecuencia = _muestras(wav)
    salida = wav

    if datos.size:
        umbral = UMBRAL_SILENCIO * 32768
        audibles = np.flatnonzero(np.abs(datos) > umbral)

        if audibles.size:
            margen_ini = int(frecuencia * dejar_inicial_ms / 1000)
            margen_fin = int(frecuencia * dejar_final_ms / 1000)
            desde = max(0, int(audibles[0]) - margen_ini)
            hasta = min(datos.size, int(audibles[-1]) + margen_fin)
            salida = _envolver(datos[desde:hasta], frecuencia)

    return salida


def normalizar_nivel(wav: bytes) -> bytes:
    """Iguala el volumen percibido y devuelve el headroom que Piper no deja.

    El RMS se mide solo sobre las muestras con voz: promediando tambien los silencios,
    una locucion con pausas largas se escalaria mas que una seguida y acabarian
    sonando distinto, que es exactamente lo que se quiere evitar.
    """

    datos, frecuencia = _muestras(wav)
    salida = wav

    if datos.size:
        flotante = datos.astype(np.float32) / 32768.0
        con_voz = flotante[np.abs(flotante) > UMBRAL_SILENCIO]

        if con_voz.size:
            rms = float(np.sqrt(np.mean(con_voz**2)))
            ganancia = RMS_OBJETIVO / rms if rms > 0 else 1.0
            percentil = float(np.percentile(np.abs(flotante), PERCENTIL_PICO))
            absoluto = float(np.max(np.abs(flotante)))

            if percentil * ganancia > PICO_MAXIMO:
                # Se cede volumen antes que dejar la onda pegada al techo. Lo que quede
                # por encima del percentil se recorta en el clip de abajo.
                ganancia = PICO_MAXIMO / percentil

            if absoluto * ganancia > 1.0:
                # Tope duro: el maximo absoluto no puede pasar del techo del formato.
                # Actua solo cuando la locucion viene mas floja de lo normal -- paso con
                # la del perfil de enfasis, que salia con menos RMS y acababa clipeada.
                # No sustituye al percentil: aqui es un limite, no el objetivo.
                ganancia = 1.0 / absoluto

            salida = _envolver(np.clip(flotante * ganancia, -1.0, 1.0) * 32767, frecuencia)

    return salida


def tratar(wav: bytes) -> bytes:
    """El tratamiento completo que recibe todo lo que sale del motor.

    El orden importa y esta medido: recortando primero, el umbral de silencio se
    aplicaba a la señal ANTES de bajarle la ganancia, asi que la cola resultante
    seguia midiendo entre 55 y 488 ms en vez de quedar clavada. Normalizando primero,
    el recorte opera sobre la señal definitiva.
    """

    return recortar_silencio(normalizar_nivel(wav))


def silencio(ms: int, frecuencia: int = FRECUENCIA_OBJETIVO) -> np.ndarray:
    return np.zeros(int(frecuencia * ms / 1000), dtype=np.int16)


def tiene_voz(wav: bytes) -> bool:
    """Si la sintesis contiene algo audible. Lo que decide si se puede cachear.

    **El fallo que evita, que es de los peores que puede tener este sistema.** Se
    encontraron siete locuciones del guion en `data/audio_cache` reducidas a 0.200 s de
    silencio digital exacto -- entre ellas `saludo`, `cierre_verde` y `cierre_amarillo` --
    con su firma correcta en el manifiesto. Correcta: el manifiesto guarda de que TEXTO
    salio el audio, y el texto no habia cambiado. Asi que el cache se consideraba vigente,
    no se re-sintetizaba nunca, y el sistema habria abierto la llamada con un saludo mudo
    sin una sola linea de log que lo llamara problema.

    Piper devuelve silencio cuando su proceso persistente queda en mal estado -- pasa al
    matar el servidor a la fuerza mientras sintetiza. La guarda de antes era
    `if clave and datos:`, y un WAV de silencio son bytes perfectamente no vacios.

    Se mide contra el mismo umbral con el que `recortar_silencio` decide que es silencio,
    para que las dos funciones no puedan discrepar sobre la palabra.
    """

    datos, _frecuencia = _muestras(wav)
    return bool(datos.size) and bool(np.any(np.abs(datos) > UMBRAL_SILENCIO * 32768))


def concatenar_wav(trozos: list[bytes], pausa_ms: int = PAUSA_ENTRE_FRAGMENTOS_MS) -> bytes:
    """Une varios WAV en uno, con una pausa deliberada entre ellos.

    Los fragmentos de un turno se envian juntos, y la pausa es lo que hace que suenen
    como una sola intervencion con respiracion en medio y no como dos archivos pegados.
    """

    utiles = [t for t in trozos if t and len(t) > 44]

    if not utiles:
        salida = b""
    elif len(utiles) == 1:
        salida = utiles[0]
    else:
        piezas: list[np.ndarray] = []
        frecuencia = FRECUENCIA_OBJETIVO
        hueco: np.ndarray | None = None

        for t in utiles:
            datos, frecuencia = _muestras(t)
            if hueco is None:
                hueco = silencio(pausa_ms, frecuencia)
            else:
                piezas.append(hueco)
            piezas.append(datos)

        salida = _envolver(np.concatenate(piezas), frecuencia)

    return salida
