"""Transcripcion con faster-whisper, endurecida contra los fallos reales de Whisper.

Este modulo se reescribio despues de que el sistema transcribiera *"si, soy yo"*
como **"Season young"** y un turno casi silencioso como **"¡Suscribete!"**. Los
dos son fallos documentados de Whisper, no accidentes, y cada uno tiene su causa:

**"Season young"** -- Whisper es debil con audio muy corto. Fue entrenado con
ventanas de 30 segundos; un clip de un segundo rellenado con 29 de silencio esta
fuera de su distribucion. La configuracion original lo empeoraba: `beam_size=1`
(busqueda voraz) y sin `initial_prompt`, que son justo las dos palancas que mas
ayudan en frases cortas.

**"¡Suscribete!"** -- alucinacion clasica sobre silencio. El corpus de
entrenamiento esta lleno de video de YouTube, asi que cuando no hay voz el modelo
emite lo que mas vio en esa situacion: "suscribete", "gracias por ver el video",
"subtitulos por...". La causa raiz es *silencio llegando al decoder*.

Cuatro defensas, de la mas importante a la menos:

1. **Silero VAD recorta el audio antes del decoder.** Se queda solo con las
   regiones con voz. Si no hay ninguna, no se llama a Whisper: se responde
   "sin voz". Asi el silencio nunca llega al modelo y la alucinacion no tiene
   donde nacer. Es lo que hace pipecat, y por lo mismo.

2. **Escalera de configuraciones validada con inferencia real.** Se prueba `medium`
   en GPU, luego `small` en GPU, luego `small` en CPU, y se baja un peldano si el
   candidato no funciona. La validacion corre una transcripcion de verdad con
   timeout, no un try/except: los dos fallos observados en esta maquina fueron
   `large-v3-turbo` llenando la VRAM (7473 de 8188 MiB) y colgandose sin excepcion,
   y cuBLAS ausente. Un `except` no atrapa un cuelgue. El resultado de la escalera
   se publica en `/api/salud`.

3. **Filtro de alucinaciones conocidas** mas umbrales de confianza
   (`no_speech_prob`, `avg_logprob`). Una frase de YouTube en una llamada clinica
   no es una transcripcion: es basura, y colarla al extractor es peor que no
   transcribir nada.

4. **Busqueda por haces y prompt de contexto clinico.** Es la palanca mas barata y
   la mas efectiva. Medido en `scripts/bench_stt.py` sobre 15 frases de paciente,
   tasa de error por palabra en frases cortas:

       small cpu, voraz, sin prompt (config original)   0.667
       small cpu, beam 5, sin prompt                    0.542
       small cpu, beam 5, con prompt clinico            0.083

   El prompt vale mas que el tamano del modelo: fija el vocabulario clinico y, de
   paso, ancla el idioma con mucha mas fuerza que el parametro `language`, que por
   si solo no evito "Season young".
"""

from __future__ import annotations

import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

FRECUENCIA = 16000


def _registrar_dlls_cuda() -> list[str]:
    """Anade al cargador de Windows las carpetas de cuBLAS y cuDNN.

    Necesario y no obvio. `ctranslate2` -- el motor de faster-whisper -- enlaza
    contra `cublas64_12.dll` y las de cuDNN en tiempo de ejecucion, y en Windows
    el cargador solo busca en el PATH del sistema y junto al ejecutable. Los
    paquetes `nvidia-cublas-cu12` y `nvidia-cudnn-cu12` las instalan dentro del
    entorno virtual, donde el cargador no mira.

    El sintoma sin esto tiene dos caras, y ambas se observaron en esta maquina:
    a veces `RuntimeError: Library cublas64_12.dll is not found`, y a veces --
    peor -- la inferencia se queda colgada para siempre sin lanzar nada.

    En Linux no hace falta: el paquete de pip deja las bibliotecas donde el
    enlazador dinamico ya busca.
    """

    registradas: list[str] = []

    if os.name == "nt":
        base = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
        if base.exists():
            for carpeta in sorted(base.glob("*/bin")):
                try:
                    os.add_dll_directory(str(carpeta))
                    registradas.append(carpeta.parent.name)
                except OSError:
                    pass
            # Tambien al PATH: algunas rutas de carga lo consultan igualmente.
            extra = os.pathsep.join(str(c) for c in base.glob("*/bin"))
            if extra:
                os.environ["PATH"] = extra + os.pathsep + os.environ.get("PATH", "")

    return registradas


DLLS_CUDA = _registrar_dlls_cuda()

# Contexto que se le pasa a Whisper en cada turno. Hace dos cosas a la vez: fija
# el dominio (vocabulario clinico en espanol) y fija el idioma con mucha mas
# fuerza que el parametro `language`, que por si solo no evito "Season young".
PROMPT_CLINICO = (
    "Llamada de seguimiento postoperatorio en Colombia. El paciente responde sobre "
    "dolor, fiebre, temperatura, movilidad, la herida quirurgica, apetito y sueno. "
    "Ejemplos: si senora, no he tenido fiebre, el dolor esta como en un seis, "
    "la herida se ve enrojecida, tengo treinta y siete cinco de temperatura."
)

# Frases que Whisper inventa cuando no hay voz. Vienen de su corpus de YouTube.
# La comparacion se hace sobre el texto normalizado y solo cuando la frase
# alucinada es practicamente todo el turno: si el paciente dice algo real y
# ademas aparece una de estas, se conserva el turno.
ALUCINACIONES = (
    "suscribete", "suscribanse", "gracias por ver", "gracias por verlo",
    "gracias por vernos", "subtitulos por", "subtitulos realizados por",
    "amara org", "amara.org", "comunidad de amara", "no olvides suscribirte",
    "dale like", "hasta la proxima", "nos vemos en el proximo video",
    "thanks for watching", "thank you for watching", "please subscribe",
    "subtitles by", "www.", "http", "musica de fondo", "aplausos",
)

# Umbrales de confianza. Un segmento que los cruza se descarta.
MAX_PROB_SIN_VOZ = 0.55      # no_speech_prob por encima de esto: no era voz
MIN_LOGPROB_MEDIO = -1.0     # avg_logprob por debajo de esto: el decoder dudaba
MIN_DURACION_S = 0.25        # por debajo de esto no hay nada que transcribir

# Por debajo de esta duracion NO se recorta por VAD, se manda el buffer completo.
#
# El recorte con Silero existe para que los silencios largos no lleguen al decoder,
# porque el silencio es lo que hace que Whisper invente frases. En un buffer de
# menos de dos segundos no hay silencio largo que quitar, y el recorte pasa de
# ganancia a riesgo: sobre una respuesta corta y dicha en voz baja, Silero puede no
# encontrar ninguna region por encima de su umbral y devolver vacio -- y entonces
# se descarta un audio que Whisper habria transcrito sin problema.
#
# El sintoma medido: "Un seis" (0.8 s) descartado como "sin voz" de forma
# intermitente. Y resulta que las respuestas de un cuestionario clinico son
# justamente asi: "No", "Normal", "Un seis", "Si senora".
#
# Dejar pasar audio al decoder no lo vuelve inseguro: despues del decoder siguen
# las dos puertas de calidad (no_speech_prob, avg_logprob) y el filtro de
# alucinaciones conocidas. Lo que se quita es una puerta ANTES del decoder que en
# este rango de duracion se equivoca mas de lo que acierta.
SIN_RECORTE_BAJO_S = 2.0

# Si Silero no encuentra voz pero el audio tiene energia de voz de verdad, se manda
# el buffer completo en vez de descartarlo. Este piso esta por encima del ruido de
# sala tipico y por debajo de una voz baja.
RMS_MINIMO_PARA_INSISTIR = 0.010

# En audio corto, `no_speech_prob` deja de ser fiable.
#
# Whisper calcula esa probabilidad sobre una ventana de 30 s: con un clip de un
# segundo, la mayor parte de la ventana es relleno y la metrica se dispara sin que
# haya nada malo en el audio. Medido con el guion de `eval/escucha.py`: "Duermo
# bien" (1.11 s) daba 0.56 contra un umbral de 0.55, y "treinta y siete cinco"
# (2.01 s) daba 0.86 -- las dos frases perfectamente pronunciadas y descartadas.
#
# En vez de bajar el umbral a ciegas, en audio corto se le pide una segunda
# opinion al decoder: si `avg_logprob` dice que estaba seguro de lo que escribio,
# gana el decoder. Solo se descarta cuando las DOS senales coinciden en que el
# audio era malo, o cuando la probabilidad de no-voz es extrema.
#
# El filtro de alucinaciones conocidas sigue corriendo antes que todo esto, que es
# la defensa que de verdad importa contra el texto inventado.
MAX_PROB_SIN_VOZ_CORTO = 0.90
MIN_LOGPROB_CORROBORA = -0.70

# Hasta aqui se considera que `no_speech_prob` no es fiable. Es un umbral DISTINTO
# de `SIN_RECORTE_BAJO_S` porque responde a otra pregunta:
#
#   SIN_RECORTE_BAJO_S      -> "hay silencio que valga la pena recortar?"  (2.0 s)
#   DURACION_NO_VOZ_DUDOSA_S -> "es fiable la probabilidad de no-voz?"      (3.5 s)
#
# Tenerlos unidos en 2.0 s costo un descarte real y absurdo: "treinta y siete
# cinco" dura 2.01 s, caia diez milisegundos por encima del corte, tomaba el camino
# estricto y se descartaba con no-voz 0.86 pese a un logprob de -0.66 -- es decir,
# con el decoder seguro de lo que habia escrito. Una temperatura perdida por un
# umbral mal puesto.
DURACION_NO_VOZ_DUDOSA_S = 3.5

TEMPERATURAS = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)


def normalizar(t: str) -> str:
    sin = "".join(
        c for c in unicodedata.normalize("NFD", t.lower())
        if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s.]", " ", sin)).strip()


def es_alucinacion(texto: str) -> str | None:
    """Devuelve el patron detectado si el turno es una alucinacion conocida."""

    base = normalizar(texto)
    encontrado: str | None = None

    if len(base) < 3:
        encontrado = "texto vacio"
    else:
        for patron in ALUCINACIONES:
            if patron in base:
                # Solo se descarta si la frase alucinada domina el turno. Un
                # paciente podria decir "gracias" de verdad.
                if len(patron) / len(base) > 0.45:
                    encontrado = patron
                    break
        if encontrado is None:
            # Repeticion patologica: el mismo token muchas veces seguidas es el
            # otro modo de fallo tipico del decoder.
            palabras = base.split()
            if len(palabras) >= 6:
                unicas = len(set(palabras))
                if unicas / len(palabras) < 0.3:
                    encontrado = "repeticion patologica"

    return encontrado


@dataclass
class Transcripcion:
    texto: str
    ms: float
    idioma: str = "es"
    probabilidad_idioma: float = 0.0
    duracion_audio_s: float = 0.0
    duracion_voz_s: float = 0.0
    segmentos: list[dict] = field(default_factory=list)
    sin_habla: bool = False
    motivo_descarte: str | None = None
    prob_sin_voz: float = 0.0
    logprob_medio: float = 0.0

    @property
    def factor_tiempo_real(self) -> float:
        return (self.ms / 1000) / self.duracion_audio_s if self.duracion_audio_s > 0 else 0.0


# Escalera de configuraciones, de la preferida a la de ultimo recurso. Se prueba
# cada una con una inferencia REAL y se baja un peldano si falla.
#
# El orden sale de dos mediciones. Primera, `scripts/bench_stt.py`: `small` con
# prompt clinico y busqueda por haces da 0.083 de error en frases cortas, contra
# 0.667 de la configuracion original -- el prompt vale mas que el tamano del
# modelo. Segunda, `nvidia-smi` durante un cuelgue: `large-v3-turbo` en float16
# ocupaba 7473 MiB de los 8188 de la GPU y la inferencia se colgaba sin lanzar
# ninguna excepcion.
#
# De ahi el criterio: modelos que quepan con holgura. Un modelo que cabe justo no
# es "mas exacto con riesgo", es un cuelgue esperando el peor momento.
ESCALERA = (
    # ~1.5 GB de VRAM. Mejor exactitud que small y sobra memoria.
    ("medium", "cuda", "int8_float16"),
    # ~1 GB. Rapidisimo y, con el prompt clinico, suficientemente exacto.
    ("small", "cuda", "float16"),
    # Sin GPU. Es la configuracion que `bench_stt.py` midio con 0.083 de error en
    # frases cortas, asi que degradar no significa degradar la exactitud.
    ("small", "cpu", "int8"),
)

# Una inferencia de prueba que tarde mas que esto se considera colgada. No basta
# con try/except: el fallo observado en GPU no lanzaba excepcion, se quedaba
# quieto para siempre, y un `except` no atrapa un cuelgue.
TIMEOUT_VALIDACION_S = 25.0


class WhisperSTT:
    def __init__(
        self,
        tamano: str | None = None,
        dispositivo: str | None = None,
        tipo_computo: str | None = None,
        dir_modelos: Path | None = None,
    ) -> None:
        self.dir_modelos = dir_modelos
        self._forzado = self._leer_forzado(tamano, dispositivo, tipo_computo)
        self.tamano, self.dispositivo, self.tipo_computo = self._forzado or ESCALERA[0]
        self._modelo = None
        self._vad = None
        self.validado = False
        self.intentos: list[dict] = []
        self.invocaciones = 0
        self.segundos_transcritos = 0.0

    @staticmethod
    def _leer_forzado(
        tamano: str | None, dispositivo: str | None, computo: str | None
    ) -> tuple[str, str, str] | None:
        """Configuracion impuesta por parametro o por entorno, si hay alguna.

        Si el operador la fija explicitamente, se respeta y no se prueba la
        escalera: forzar algo y que el sistema lo cambie por su cuenta seria
        desconcertante.
        """

        t = tamano or os.environ.get("CENTINELA_STT_MODEL") or ""
        d = dispositivo or os.environ.get("CENTINELA_STT_DEVICE") or ""
        c = computo or os.environ.get("CENTINELA_STT_COMPUTE") or ""

        if t or d or c:
            forzado = (
                t or "small",
                d or "cpu",
                c or ("float16" if d == "cuda" else "int8"),
            )
        else:
            forzado = None
        return forzado

    # ------------------------------------------------------------------

    def _cargar(self, tamano: str, dispositivo: str, computo: str):
        from faster_whisper import WhisperModel

        kwargs = {"device": dispositivo, "compute_type": computo}
        if self.dir_modelos:
            kwargs["download_root"] = str(self.dir_modelos)
        return WhisperModel(tamano, **kwargs)

    def _probar_inferencia(self, modelo) -> tuple[bool, str]:
        """Corre una transcripcion de verdad, con timeout, en un hilo aparte.

        El hilo es lo que hace esto util: si la inferencia se cuelga -- lo que pasa
        cuando el modelo no cabe en la VRAM --, el hilo se queda bloqueado pero el
        proceso principal recupera el control y puede bajar un peldano. El hilo
        colgado se abandona: es la unica salida cuando la libreria nativa no
        devuelve el control, y se documenta como tal en vez de disimularlo.
        """

        import threading

        resultado: dict = {}

        def trabajo() -> None:
            try:
                rng = np.random.default_rng(0)
                # Habla sintetica sencilla: no importa lo que transcriba, importa
                # que la inferencia complete. Silencio no serviria -- se descarta
                # antes de llegar al modelo y no probaria nada.
                t = np.arange(FRECUENCIA * 2) / FRECUENCIA
                audio = (
                    0.3 * np.sin(2 * np.pi * 160 * t)
                    + 0.2 * np.sin(2 * np.pi * 780 * t)
                    + rng.normal(0, 0.01, len(t))
                ).astype(np.float32)
                audio *= (0.5 + 0.5 * np.sin(2 * np.pi * 3.5 * t)).astype(np.float32)
                segmentos, _info = modelo.transcribe(
                    audio, language="es", beam_size=5, vad_filter=False,
                )
                list(segmentos)  # fuerza la ejecucion del generador
                resultado["ok"] = True
            except Exception as e:  # noqa: BLE001
                resultado["error"] = f"{type(e).__name__}: {e}"

        hilo = threading.Thread(target=trabajo, daemon=True)
        hilo.start()
        hilo.join(timeout=TIMEOUT_VALIDACION_S)

        if hilo.is_alive():
            veredicto = (False, f"la inferencia se colgo (>{TIMEOUT_VALIDACION_S:.0f}s)")
        elif resultado.get("ok"):
            veredicto = (True, "")
        else:
            veredicto = (False, resultado.get("error", "fallo sin detalle"))

        return veredicto

    def preparar(self) -> None:
        """Elige la mejor configuracion que de verdad funciona en esta maquina.

        Se llama al arrancar, no en el primer turno: descubrir aqui que la GPU no
        sirve cuesta unos segundos de arranque; descubrirlo en el primer turno del
        jurado cuesta la demo.
        """

        if self._modelo is not None:
            return

        candidatas = [self._forzado] if self._forzado else list(ESCALERA)

        for tamano, dispositivo, computo in candidatas:
            etiqueta = f"{tamano}/{dispositivo}/{computo}"
            intento: dict = {"config": etiqueta}
            t0 = time.perf_counter()
            try:
                modelo = self._cargar(tamano, dispositivo, computo)
            except Exception as e:  # noqa: BLE001
                intento["resultado"] = f"no carga: {type(e).__name__}"
                self.intentos.append(intento)
                print(f"[stt] {etiqueta}: no carga ({type(e).__name__})")
                continue

            ok, motivo = self._probar_inferencia(modelo)
            intento["ms_validacion"] = round((time.perf_counter() - t0) * 1000)

            if ok:
                intento["resultado"] = "validada"
                self.intentos.append(intento)
                self._modelo = modelo
                self.tamano, self.dispositivo, self.tipo_computo = tamano, dispositivo, computo
                self.validado = True
                print(f"[stt] usando {etiqueta} (validada en "
                      f"{intento['ms_validacion']} ms)")
                break

            intento["resultado"] = motivo
            self.intentos.append(intento)
            print(f"[stt] {etiqueta}: {motivo}; bajando un peldano")
            del modelo

        if self._modelo is None:
            # Ni el ultimo peldano funciono. Se carga sin validar para no dejar el
            # sistema sin STT: es mejor un STT que quizas falle que ninguno.
            print("[stt] ninguna configuracion se valido; cargando small/cpu sin validar")
            self.tamano, self.dispositivo, self.tipo_computo = ESCALERA[-1]
            self._modelo = self._cargar(*ESCALERA[-1])

    @property
    def modelo(self):
        if self._modelo is None:
            self.preparar()
        return self._modelo

    @property
    def degradado_a_cpu(self) -> bool:
        return self.dispositivo == "cpu" and self._forzado is None

    @property
    def vad(self):
        """Silero VAD, el mismo que trae faster-whisper."""

        if self._vad is None:
            from faster_whisper.vad import VadOptions, get_speech_timestamps

            self._vad = (get_speech_timestamps, VadOptions)
        return self._vad

    def estado(self) -> dict:
        return {
            "motor": "faster-whisper",
            "modelo": self.tamano,
            "dispositivo": f"{self.dispositivo}/{self.tipo_computo}",
            "cargado": self._modelo is not None,
            "validado_con_inferencia_real": self.validado,
            "invocaciones": self.invocaciones,
            "segundos_transcritos": round(self.segundos_transcritos, 1),
            "degradado_a_cpu": self.degradado_a_cpu,
            "configuracion_forzada": self._forzado is not None,
            # El historial de la escalera se publica: si la GPU no sirvio, el
            # jurado ve por que y no tiene que adivinarlo.
            "intentos": self.intentos,
            "vad": (
                f"silero recorta el audio antes del decoder solo si dura "
                f"{SIN_RECORTE_BAJO_S}s o mas; por debajo va entero"
            ),
            "defensas": [
                f"recorte a regiones con voz (solo sobre {SIN_RECORTE_BAJO_S}s+)",
                "busqueda por haces (beam 5) y prompt clinico",
                "filtro de alucinaciones conocidas",
                f"no_speech_prob < {MAX_PROB_SIN_VOZ}",
                f"avg_logprob > {MIN_LOGPROB_MEDIO}",
            ],
        }

    # ------------------------------------------------------------------

    def _recortar_a_voz(self, muestras: np.ndarray) -> tuple[np.ndarray, float]:
        """Deja solo las regiones con voz, segun Silero VAD.

        Es la defensa principal contra la alucinacion: si el silencio no llega al
        decoder, el decoder no tiene ocasion de inventar una frase de YouTube.
        """

        try:
            get_speech_timestamps, VadOptions = self.vad
            opciones = VadOptions(
                # Umbral de Silero. 0.5 es el valor por defecto; se sube un poco
                # porque el cliente ya hizo una primera puerta y aqui interesa
                # descartar respiracion y ruido de teclado.
                threshold=0.5,
                min_speech_duration_ms=200,
                max_speech_duration_s=float("inf"),
                min_silence_duration_ms=300,
                # Margen alrededor de cada region con voz, para no cortar la
                # primera ni la ultima silaba.
                speech_pad_ms=200,
            )
            tramos = get_speech_timestamps(muestras, opciones)
        except Exception:  # noqa: BLE001
            # Si Silero falla por cualquier motivo, se sigue con el audio completo:
            # perder el VAD degrada la calidad, perder el turno seria peor.
            tramos = []

        if not tramos:
            recortado = np.array([], dtype=np.float32)
            segundos = 0.0
        else:
            trozos = [muestras[t["start"]:t["end"]] for t in tramos]
            recortado = np.concatenate(trozos).astype(np.float32)
            segundos = len(recortado) / FRECUENCIA

        return recortado, segundos

    # ------------------------------------------------------------------

    def transcribir(self, muestras: np.ndarray, frecuencia: int = FRECUENCIA) -> Transcripcion:
        """Transcribe audio mono float32 en [-1, 1] a 16 kHz."""

        t0 = time.perf_counter()
        duracion = len(muestras) / frecuencia
        # Contadores de invocacion. Existen porque sin ellos un turno lento no se puede
        # diagnosticar: el modelo atiende de uno en uno, asi que la latencia de UN turno
        # puede ser la cola de transcripciones que otro dejo encoladas. Con el desglose
        # por etapa no se distingue "transcribir es lento" de "habia cola".
        self.invocaciones += 1
        self.segundos_transcritos += duracion

        if duracion < MIN_DURACION_S:
            return Transcripcion(
                texto="", ms=(time.perf_counter() - t0) * 1000,
                duracion_audio_s=round(duracion, 2), sin_habla=True,
                motivo_descarte=f"audio de {duracion:.2f}s, por debajo del minimo",
            )

        rms = energia_rms(muestras)

        if duracion < SIN_RECORTE_BAJO_S:
            # Respuesta corta: no hay silencio largo que quitar. Va entera.
            voz, segundos_voz = muestras, duracion
        else:
            voz, segundos_voz = self._recortar_a_voz(muestras)

            if segundos_voz < MIN_DURACION_S and rms >= RMS_MINIMO_PARA_INSISTIR:
                # Silero dice que no hay voz, pero hay energia de voz. Antes de
                # tirar el turno se le da la oportunidad al decoder, que tiene su
                # propio criterio (`no_speech_prob`) y es mejor que este.
                voz, segundos_voz = muestras, duracion

        if segundos_voz < MIN_DURACION_S:
            # Ni region con voz ni energia. Aqui se corta el camino: no se invoca a
            # Whisper, asi que no puede alucinar.
            return Transcripcion(
                texto="", ms=(time.perf_counter() - t0) * 1000,
                duracion_audio_s=round(duracion, 2), duracion_voz_s=round(segundos_voz, 2),
                sin_habla=True,
                motivo_descarte=(
                    f"Silero VAD no encontro voz y la energia es baja (rms {rms:.4f})"
                ),
            )

        segmentos, info = self.modelo.transcribe(
            voz,
            language="es",
            task="transcribe",
            # Busqueda por haces: medido, baja el error en frases cortas.
            beam_size=5,
            # Prompt de dominio: fija vocabulario clinico y refuerza el idioma.
            initial_prompt=PROMPT_CLINICO,
            # Cadena de temperaturas: si el decodificado sale con baja confianza,
            # se reintenta con mas temperatura en vez de devolver el primer
            # resultado malo. Con una sola temperatura este mecanismo se apaga.
            temperature=TEMPERATURAS,
            compression_ratio_threshold=2.4,
            log_prob_threshold=MIN_LOGPROB_MEDIO,
            no_speech_threshold=0.6,
            # El audio ya viene recortado por Silero: aplicar el filtro otra vez
            # solo puede quitar principio y final de palabra.
            vad_filter=False,
            condition_on_previous_text=False,
        )

        piezas: list[str] = []
        detalle: list[dict] = []
        probs_sin_voz: list[float] = []
        logprobs: list[float] = []

        for s in segmentos:
            piezas.append(s.text)
            prob_sin_voz = float(getattr(s, "no_speech_prob", 0.0) or 0.0)
            logprob = float(getattr(s, "avg_logprob", 0.0) or 0.0)
            probs_sin_voz.append(prob_sin_voz)
            logprobs.append(logprob)
            detalle.append({
                "inicio": round(s.start, 2),
                "fin": round(s.end, 2),
                "texto": s.text.strip(),
                "prob_sin_voz": round(prob_sin_voz, 3),
                "logprob_medio": round(logprob, 3),
            })

        texto = " ".join(p.strip() for p in piezas).strip()
        ms = (time.perf_counter() - t0) * 1000
        prob_sin_voz = max(probs_sin_voz) if probs_sin_voz else 0.0
        logprob_medio = min(logprobs) if logprobs else 0.0

        motivo = self._evaluar_calidad(texto, prob_sin_voz, logprob_medio, duracion)

        return Transcripcion(
            texto="" if motivo else texto,
            ms=round(ms, 2),
            idioma=getattr(info, "language", "es"),
            probabilidad_idioma=round(getattr(info, "language_probability", 0.0), 3),
            duracion_audio_s=round(duracion, 2),
            duracion_voz_s=round(segundos_voz, 2),
            segmentos=detalle,
            sin_habla=bool(motivo),
            motivo_descarte=motivo,
            prob_sin_voz=round(prob_sin_voz, 3),
            logprob_medio=round(logprob_medio, 3),
        )

    @staticmethod
    def _evaluar_calidad(
        texto: str, prob_sin_voz: float, logprob_medio: float, duracion: float = 99.0
    ) -> str | None:
        """Decide si la transcripcion es utilizable. Devuelve el motivo si no.

        `duracion` cambia el criterio de no-voz: en audio corto esa probabilidad no
        es fiable y hace falta que el decoder la corrobore. El razonamiento esta
        junto a `MAX_PROB_SIN_VOZ_CORTO`.
        """

        motivo: str | None = None
        corto = duracion < DURACION_NO_VOZ_DUDOSA_S

        if not texto.strip():
            motivo = "el decoder no produjo texto"
        else:
            patron = es_alucinacion(texto)
            if patron:
                motivo = f"alucinacion conocida de Whisper ({patron}): {texto[:60]!r}"
            elif corto:
                if prob_sin_voz > MAX_PROB_SIN_VOZ_CORTO:
                    motivo = (
                        f"audio corto y probabilidad de no-voz extrema "
                        f"({prob_sin_voz:.2f} > {MAX_PROB_SIN_VOZ_CORTO})"
                    )
                elif (
                    prob_sin_voz > MAX_PROB_SIN_VOZ
                    and logprob_medio < MIN_LOGPROB_CORROBORA
                ):
                    motivo = (
                        f"audio corto: no-voz alta ({prob_sin_voz:.2f}) y el decoder "
                        f"tampoco estaba seguro (logprob {logprob_medio:.2f})"
                    )
                elif logprob_medio < MIN_LOGPROB_MEDIO:
                    motivo = (
                        f"el decoder dudaba demasiado "
                        f"(logprob medio {logprob_medio:.2f} < {MIN_LOGPROB_MEDIO})"
                    )
            elif prob_sin_voz > MAX_PROB_SIN_VOZ:
                motivo = (
                    f"probabilidad de que no fuera voz demasiado alta "
                    f"({prob_sin_voz:.2f} > {MAX_PROB_SIN_VOZ})"
                )
            elif logprob_medio < MIN_LOGPROB_MEDIO:
                motivo = (
                    f"el decoder dudaba demasiado "
                    f"(logprob medio {logprob_medio:.2f} < {MIN_LOGPROB_MEDIO})"
                )

        return motivo

    def calentar(self) -> None:
        """Elige y valida la configuracion, y deja el modelo listo en memoria.

        `preparar()` ya corre una inferencia real, asi que al volver de aqui el
        primer turno del jurado no paga ni la carga del modelo ni el descubrimiento
        de que la GPU no servia.
        """

        self.preparar()


# --------------------------------------------------------------------------
# Utilidades de audio para el WebSocket
# --------------------------------------------------------------------------

def pcm16_a_float32(datos: bytes) -> np.ndarray:
    """PCM 16 bits little-endian -> float32 en [-1, 1]."""

    if len(datos) < 2:
        return np.array([], dtype=np.float32)
    # Se descarta un byte impar final: un frame partido rompe el desempaquetado.
    utiles = datos[: len(datos) - (len(datos) % 2)]
    enteros = np.frombuffer(utiles, dtype="<i2")
    return (enteros.astype(np.float32) / 32768.0).copy()


def energia_rms(muestras: np.ndarray) -> float:
    valor = float(np.sqrt(np.mean(np.square(muestras)))) if len(muestras) else 0.0
    return valor
