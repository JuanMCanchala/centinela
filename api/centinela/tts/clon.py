"""La voz clonada, leida del disco antes de encender ningun motor.

**Por que no se sintetiza en la llamada.** Chatterbox clona bien y corre a RTF 4 en CPU:
generar una frase de diez segundos cuesta cuarenta. Y tampoco puede vivir en el venv de
ejecucion, porque arrastra 2.5 GB de torch con CUDA y eso solo reventaria la compuerta G2 de
quince minutos. Asi que el clon es una **herramienta de construccion**: se renderiza fuera
con `scripts/render_clon.py`, el WAV se versiona, y en ejecucion esto solo lee disco.

**El cache va direccionado por contenido.** La clave es el hash del texto tal como lo recibe
el motor --despues de `para_voz`-- y no el nombre de la locucion. Eso compra tres cosas:

  - Cubre cualquier texto, no solo el guion. Una respuesta del RAG pre-renderizada se
    encuentra igual que un saludo, sin inventarle una clave.
  - No toca `PiperTTS._firma` ni su manifiesto, asi que no puede invalidar el cache de Piper
    ni quedar invalidado por el.
  - Si alguien edita el guion, el hash cambia, hay fallo de cache y se oye a Piper. El
    defecto se vuelve **audible** en vez de servir audio viejo con texto nuevo, que es el
    mismo fallo que ya hubo que corregir una vez en `_firma`.

**Los fallos se cuentan.** Un fallo no es un error --el respaldo funciona-- pero si es un
cambio de hablante a mitad de llamada, que es de las cosas que mas delatan a una maquina.
`estado()` los publica para que se vean en la consola y en las metricas, en vez de que pasen
callados.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..obs.log import log
from .hablado import para_voz

DIR_CLON = Path(__file__).resolve().parents[3] / "data" / "audio_clon"
MANIFIESTO = "manifiesto.json"


def clave_de(texto: str) -> str:
    """El hash del texto que el motor va a leer.

    Sobre `para_voz(texto)` y no sobre el texto crudo: "38.5 °C" y "treinta y ocho y medio
    grados" suenan igual y deben encontrar el mismo archivo. Es la misma normalizacion que
    usa `PiperTTS._firma`, a proposito.
    """

    return hashlib.sha256(para_voz(texto).encode("utf-8")).hexdigest()[:16]


@dataclass
class VozClonada:
    """Lee los WAV pre-renderizados. No sintetiza nada."""

    directorio: Path = DIR_CLON
    _memoria: dict[str, bytes] = field(default_factory=dict, repr=False)
    _manifiesto: dict | None = field(default=None, repr=False)
    aciertos: int = 0
    fallos: int = 0
    textos_sin_clonar: list[str] = field(default_factory=list, repr=False)

    @property
    def manifiesto(self) -> dict:
        if self._manifiesto is None:
            archivo = self.directorio / MANIFIESTO
            leido = {}
            if archivo.exists():
                try:
                    leido = json.loads(archivo.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError) as e:
                    log("manifiesto_clon_ilegible", nivel="error", error=str(e))
                    leido = {}
            self._manifiesto = leido
        return self._manifiesto

    @property
    def disponible(self) -> bool:
        return bool(self.manifiesto.get("locuciones"))

    @property
    def voz(self) -> str:
        return str(self.manifiesto.get("voz") or "sin_clon")

    def buscar(self, texto: str) -> bytes | None:
        """El WAV clonado de este texto, o None si no se pre-renderizo."""

        clave = clave_de(texto)
        encontrado = self._memoria.get(clave)

        if encontrado is None:
            entrada = (self.manifiesto.get("locuciones") or {}).get(clave)
            archivo = self.directorio / f"{clave}.wav"
            if entrada and archivo.exists():
                try:
                    encontrado = archivo.read_bytes()
                    self._memoria[clave] = encontrado
                except OSError as e:
                    log("wav_clonado_ilegible", nivel="error", clave=clave, error=str(e))

        if encontrado:
            self.aciertos += 1
        else:
            self.fallos += 1
            # Se guarda el texto, no solo la cuenta: para volver a renderizar hace falta
            # saber QUE falto, y una cuenta suelta no dice nada accionable.
            recorte = texto[:120]
            if recorte not in self.textos_sin_clonar:
                self.textos_sin_clonar.append(recorte)
            log("voz_clonada_sin_entrada", clave=clave, texto=recorte)

        return encontrado

    def estado(self) -> dict:
        pedidos = self.aciertos + self.fallos
        return {
            "voz": self.voz,
            "disponible": self.disponible,
            "locuciones": len(self.manifiesto.get("locuciones") or {}),
            "aciertos": self.aciertos,
            "fallos": self.fallos,
            "cobertura_pct": (
                round(self.aciertos / pedidos * 100, 1) if pedidos else None
            ),
            # El respaldo es un cambio de hablante, asi que conviene tenerlo delante.
            "al_fallar": "se sintetiza con Piper, y la llamada cambia de voz",
            "textos_sin_clonar": self.textos_sin_clonar[:10],
        }
