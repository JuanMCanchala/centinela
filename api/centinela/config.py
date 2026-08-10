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
    # A `0` el agente habla enteramente con Piper. Es el interruptor que se busca cuando algo
    # suena raro en vivo: permite descartar la voz clonada sin editar codigo ni borrar WAV.
    usar_voz_clonada: bool = os.environ.get("CENTINELA_VOZ_CLONADA", "1") == "1"

    # --- escalamiento ---
    #
    # Una llamada sin actividad durante este tiempo se cierra sola. Existe porque el
    # cliente puede desaparecer sin cerrar el socket (pestana cerrada de golpe, red
    # caida, portatil suspendido) y una llamada abierta para siempre es una alerta
    # que nunca sale.
    timeout_llamada_s: int = int(os.environ.get("CENTINELA_TIMEOUT_LLAMADA_S", "180"))
    sla_rojo_min: int = int(os.environ.get("CENTINELA_SLA_ROJO_MIN", "15"))
    sla_amarillo_h: int = int(os.environ.get("CENTINELA_SLA_AMARILLO_H", "24"))
    webhook_alertas: str = os.environ.get("CENTINELA_WEBHOOK_ALERTAS", "")
    secreto_webhook: str = os.environ.get("CENTINELA_SECRETO_WEBHOOK", "")

    # --- acceso ---
    #
    # Vacio a proposito. La compuerta G2 del reto da 15 minutos para levantar la
    # solucion siguiendo solo el README, y pedir configurar un token para ver la
    # consola gasta ese presupuesto en nada. Si se define, protege los endpoints que
    # modifican algo Y la llamada entera, canal de voz incluido. Un despliegue clinico
    # real necesita identidad por persona, no un secreto compartido, y eso esta
    # declarado en docs/operacion.md.
    #
    # Tiene que ser UNA PALABRA. El canal de voz lo lleva en el subprotocolo del
    # WebSocket -- ver `main.token_del_socket` -- y ahi no caben espacios ni comas.
    # `token_transportable` lo comprueba y el arranque lo avisa, porque el sintoma de
    # un token con un espacio seria un canal de voz que no conecta sin decir por que.
    token_consola: str = os.environ.get("CENTINELA_TOKEN", "")

    # Tope de subida de documentos. Un PDF de guia clinica no pasa de unas decenas de
    # megas; el limite esta para que una peticion no llene el disco del servidor.
    max_mb_documento: int = int(os.environ.get("CENTINELA_MAX_MB_DOC", "64"))

    # --- conversacion ---
    #
    # El paciente puede cortarle la palabra al agente. El interruptor existe para
    # poder apagarlo en una demo sin tocar codigo: apagado, la voz del agente sale
    # entera y el turno cierra a los 900 ms de siempre, que es la conducta que
    # midieron todas las metricas anteriores.
    bargein: bool = os.environ.get("CENTINELA_BARGEIN", "1") == "1"
    # Cuanto por encima del eco observado hay que hablar para cortar, y con que
    # percentil se resume el eco. Los dos valores por defecto son los que eligio el
    # barrido de `eval/bargein.py`, y `tests/test_bargein.py` comprueba que no se
    # separen de los del modulo: tener el numero medido en un sitio y el que corre en
    # otro es peor que no medirlo.
    bargein_margen_eco: float = float(os.environ.get("CENTINELA_BARGEIN_MARGEN_ECO", "1.8"))
    # Cuanto audio se acumula antes de preguntarle al STT si eso era voz. Es tambien
    # lo que dura el bache cuando resulta que era una tos.
    bargein_ms_confirmacion: float = float(
        os.environ.get("CENTINELA_BARGEIN_MS_CONF", "250")
    )

    # El turno del paciente cierra en cuanto su respuesta se sostiene sola, con este
    # plazo como piso. El TECHO no esta aqui: lo pone el VAD del navegador, que es
    # quien mide el silencio en el microfono. Poner aqui una variable de techo seria
    # ofrecer un mando que no esta conectado a nada.
    cierre_adaptativo: bool = os.environ.get("CENTINELA_CIERRE_ADAPTATIVO", "1") == "1"
    cierre_min_ms: float = float(os.environ.get("CENTINELA_CIERRE_MIN_MS", "450"))

    # Cuanto se espera a que el navegador vuelva antes de dar la llamada por colgada.
    # Un bache de red de dos segundos no es un paciente que cuelga, y hasta ahora las
    # dos cosas se trataban igual: cierre forzado como "interrumpida". Con esta ventana
    # la llamada sobrevive al bache con su estado clinico intacto.
    #
    # No debilita ninguna garantia: el cierre forzado sigue ocurriendo, solo se retrasa
    # hasta que la ventana expira. Y el barredor de inactividad
    # (`timeout_llamada_s`) sigue siendo la red de seguridad de ultimo recurso.
    gracia_reconexion_s: float = float(
        os.environ.get("CENTINELA_GRACIA_RECONEXION_S", "20")
    )

    # Que el agente reaccione cuando el paciente se queda callado, en vez de esperar en
    # silencio hasta que el barredor de inactividad cierre la llamada a los 180 s. La
    # escalera de peldanos y el porque de cada uno estan en `dialog/silencio.py`.
    #
    # A `0` la conducta es la de antes: nadie dice nada y la llamada dura lo que dure. Se
    # deja el mando porque los arneses que miden latencia no quieren un agente hablando
    # solo entre turnos.
    silencio: bool = os.environ.get("CENTINELA_SILENCIO", "1") == "1"

    @property
    def ruta_metricas(self) -> Path:
        return self.dir_runtime / "metricas.jsonl"

    def asegurar_directorios(self) -> None:
        for d in (self.dir_index, self.dir_runtime, self.dir_audio_cache, self.dir_subidas):
            d.mkdir(parents=True, exist_ok=True)

    def token_transportable(self) -> bool:
        """Si el token configurado puede viajar en el subprotocolo del WebSocket.

        Un subprotocolo es un `token` de HTTP: sin espacios, sin comas y sin
        caracteres de control. Los separadores son los que romperian la cabecera; el
        resto se deja pasar en vez de imponer una lista blanca, porque un token
        rechazado por un guion seria una molestia sin motivo.
        """

        prohibidos = set(' \t",;()<>@\\/[]?={}')
        sano = bool(self.token_consola) and not (
            set(self.token_consola) & prohibidos
        ) and self.token_consola.isprintable()
        return sano

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
            "escalamiento": {
                "timeout_llamada_s": self.timeout_llamada_s,
                "sla_rojo_min": self.sla_rojo_min,
                "sla_amarillo_h": self.sla_amarillo_h,
                "canales": ["archivo"] + (["webhook"] if self.webhook_alertas else []),
            },
            "conversacion": {
                "bargein": self.bargein,
                "bargein_margen_eco": self.bargein_margen_eco,
                "bargein_ms_confirmacion": self.bargein_ms_confirmacion,
                "cierre_adaptativo": self.cierre_adaptativo,
                "cierre_min_ms": self.cierre_min_ms,
                "gracia_reconexion_s": self.gracia_reconexion_s,
                "silencio": self.silencio,
            },
            "acceso_protegido": bool(self.token_consola),
        }


config = Config()
