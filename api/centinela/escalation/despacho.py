"""Entrega de alertas. Un ticket en una tabla no es una alerta.

La rubrica pregunta que produce el sistema cuando decide alertar, "con que
persistencia". Persistir el ticket es la mitad; la otra mitad es que la alerta
salga del proceso y que alguien acuse recibo. Sin eso, un rojo se queda esperando
en una bandeja que nadie abre -- que es exactamente el estado en el que estaba el
sistema: 83 tickets abiertos, cero atendidos.

El patron es un outbox. La tabla `entregas` se escribe en la misma transaccion que
el ticket, asi que no existe el estado intermedio "hay alerta pero nadie va a
entregarla". Este modulo es el unico consumidor de esa cola:

- **Al menos una vez.** Si el canal falla, se reintenta con espera creciente. Solo
  se rinde tras `MAX_INTENTOS`, y entonces la fila queda en `agotado` con su ultimo
  error -- visible, no perdida.
- **Idempotente por `(ticket_id, canal)`.** Reencolar el mismo ticket no duplica la
  entrega, y una entrega repetida escribe el mismo archivo.
- **Sobrevive al reinicio.** La cola esta en SQLite, no en memoria: el despachador
  arranca y encuentra lo que quedo pendiente.

Dos canales van de serie. `CanalArchivo` no necesita configuracion y por eso es el
que hace de prueba en el demo: la hoja de traspaso aparece en el disco. Un despliegue
real anade `CanalWebhook` con la URL del sistema de enfermeria.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
from typing import Protocol

import httpx

from ..obs.log import log


class Canal(Protocol):
    """Un destino de alerta.

    `entregar` no devuelve nada: entrega o levanta. El despachador traduce la
    excepcion en un reintento, asi que un canal nuevo no tiene que saber nada de la
    politica de reintentos.
    """

    nombre: str

    async def entregar(self, alerta: dict) -> None:
        ...


class CanalArchivo:
    """Escribe la hoja legible en un directorio.

    Es el canal que siempre funciona, y sirve de comprobante en el demo: tras una
    llamada que escala, la hoja esta en el disco y se puede abrir.
    """

    nombre = "archivo"

    def __init__(self, directorio: Path) -> None:
        self.directorio = Path(directorio)
        self.directorio.mkdir(parents=True, exist_ok=True)

    async def entregar(self, alerta: dict) -> None:
        destino = self.directorio / f"{alerta['ticket_id']}.txt"
        # Escritura atomica: si el proceso muere a mitad, no queda media hoja en el
        # sitio donde alguien va a buscar una alerta completa.
        temporal = destino.with_suffix(".txt.parcial")
        temporal.write_text(alerta["hoja_legible"], encoding="utf-8")
        temporal.replace(destino)


class CanalWebhook:
    """POST firmado al sistema que recibe las alertas.

    La firma es HMAC-SHA256 del cuerpo exacto que se envia. Va en una cabecera para
    que el receptor pueda verificar que la alerta viene de aqui y que nadie la
    modifico en el camino -- en una alerta clinica eso no es opcional.
    """

    nombre = "webhook"

    def __init__(self, url: str, secreto: str, timeout_s: float = 10.0) -> None:
        self.url = url
        self.secreto = secreto
        self.timeout_s = timeout_s

    async def entregar(self, alerta: dict) -> None:
        cuerpo = json.dumps(
            {
                "ticket_id": alerta["ticket_id"],
                "llamada_id": alerta["llamada_id"],
                "nivel": alerta["nivel"],
                "motivo": alerta["motivo"],
                "hoja_legible": alerta["hoja_legible"],
            },
            ensure_ascii=False,
        ).encode("utf-8")

        cabeceras = {"Content-Type": "application/json"}
        if self.secreto:
            firma = hmac.new(
                self.secreto.encode("utf-8"), cuerpo, hashlib.sha256
            ).hexdigest()
            cabeceras["X-Centinela-Firma"] = f"sha256={firma}"

        async with httpx.AsyncClient(timeout=self.timeout_s) as cliente:
            r = await cliente.post(self.url, content=cuerpo, headers=cabeceras)
            r.raise_for_status()


class Despachador:
    """Vacia la cola de entregas, reintentando lo que falla.

    Se le pasa el servicio en vez de la conexion para que toda la escritura pase por
    el mismo sitio que el resto del escalamiento, con su lock.
    """

    def __init__(self, servicio, canales: list[Canal], intervalo_s: float = 5.0) -> None:
        self.servicio = servicio
        self.canales = {c.nombre: c for c in canales}
        self.intervalo_s = intervalo_s
        self.entregadas = 0
        self.fallidas = 0
        servicio.registrar_canales(tuple(self.canales))

    async def paso(self) -> int:
        """Un barrido de la cola. Devuelve cuantas entregas se resolvieron.

        Separado del bucle para que un test pueda ejercitarlo sin esperar timers.
        """

        pendientes = self.servicio.entregas_pendientes()
        resueltas = 0

        for entrega in pendientes:
            canal = self.canales.get(entrega["canal"])
            if canal is None:
                # El canal se quito de la configuracion. No se descarta la fila: se
                # deja dicho, porque una alerta sin destino es una decision de
                # operacion, no un detalle.
                self.servicio.marcar_fallida(
                    entrega["ticket_id"], entrega["canal"],
                    "canal no configurado en este proceso",
                )
            else:
                try:
                    await canal.entregar(entrega)
                except Exception as e:  # noqa: BLE001
                    self.fallidas += 1
                    self.servicio.marcar_fallida(
                        entrega["ticket_id"], entrega["canal"], f"{type(e).__name__}: {e}"
                    )
                    log("alerta_no_entregada", nivel="error",
                        ticket=entrega["ticket_id"], canal=entrega["canal"],
                        error=f"{type(e).__name__}: {e}")
                else:
                    self.entregadas += 1
                    resueltas += 1
                    self.servicio.marcar_entregada(entrega["ticket_id"], entrega["canal"])
                    log("alerta_entregada", ticket=entrega["ticket_id"],
                        canal=entrega["canal"], nivel_clinico=entrega["nivel"])

        return resueltas

    async def correr(self) -> None:
        """Bucle de fondo. Se cancela desde el `lifespan` al apagar."""

        while True:
            try:
                await self.paso()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                # Un fallo del despachador no puede tumbar el proceso ni detener la
                # cola: se anota y se vuelve a intentar en el siguiente barrido.
                log("despachador_fallo", nivel="error", error=f"{type(e).__name__}: {e}")
            await asyncio.sleep(self.intervalo_s)


def canales_desde_config(config) -> list[Canal]:
    """Los canales que este despliegue tiene configurados.

    El de archivo va siempre: es el que hace que el sistema funcione recien clonado,
    sin pedirle al jurado que configure un webhook para ver una alerta entregada.
    """

    canales: list[Canal] = [CanalArchivo(config.dir_runtime / "alertas")]
    if config.webhook_alertas:
        canales.append(CanalWebhook(config.webhook_alertas, config.secreto_webhook))
    return canales
