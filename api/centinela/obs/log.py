"""Registro estructurado, una linea JSON por evento.

El sistema tenia `print` sueltos. Sirven mientras se desarrolla y dejan de servir en
cuanto hay que responder una pregunta de operacion: *que paso en la llamada
`abc123`*. Un `print` no se puede filtrar por llamada, ni contar, ni correlacionar
con el ticket que salio de ella.

Aqui cada evento es una linea JSON con su nombre y sus campos. Dos decisiones:

- **Sin dependencias.** `logging` de la biblioteca estandar, un handler, un
  formateador que serializa el `extra`. Nada que instalar y nada que configurar para
  que el jurado lo vea funcionando.
- **`llamada_id` es un campo de primera clase**, no texto dentro del mensaje. Es la
  clave con la que se reconstruye una llamada completa: turnos, decision, ticket y
  entrega de la alerta.

No se registra el texto de lo que dijo el paciente. La transcripcion vive en la base
de datos de llamadas, que es el registro clinico; duplicarla en un log de operacion
la esparce por sitios con retenciones distintas.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

NOMBRE = "centinela"

_NIVELES = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "aviso": logging.WARNING,
    "error": logging.ERROR,
}


class FormateadorJSON(logging.Formatter):
    def format(self, registro: logging.LogRecord) -> str:
        cuerpo = {
            "t": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "nivel": registro.levelname.lower(),
            "evento": registro.getMessage(),
        }
        extra = getattr(registro, "campos", None)
        if extra:
            cuerpo.update(extra)
        if registro.exc_info:
            cuerpo["excepcion"] = self.formatException(registro.exc_info)
        return json.dumps(cuerpo, ensure_ascii=False, default=str)


def _preparar() -> logging.Logger:
    registrador = logging.getLogger(NOMBRE)
    if not registrador.handlers:
        salida = logging.StreamHandler(sys.stderr)
        salida.setFormatter(FormateadorJSON())
        registrador.addHandler(salida)
        registrador.setLevel(logging.INFO)
        # Sin esto la linea sale dos veces: una con nuestro formato y otra con el del
        # root que uvicorn configura por su cuenta.
        registrador.propagate = False
    return registrador


_registrador = _preparar()


def log(evento: str, nivel: str = "info", **campos: object) -> None:
    """Escribe un evento.

    `evento` es un nombre estable en snake_case -- `alerta_entregada`,
    `llamada_cerrada_por_timeout` -- para que se pueda contar y filtrar. Lo variable
    va en los campos, nunca interpolado en el nombre.
    """

    _registrador.log(
        _NIVELES.get(nivel, logging.INFO), evento, extra={"campos": campos}
    )
