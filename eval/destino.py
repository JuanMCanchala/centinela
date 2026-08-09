"""A qué servidor apuntan los arneses. Un solo sitio donde decidirlo.

Por qué existe. Los arneses no se ponían de acuerdo: `humo`, `redteam`, `rag_cobertura`,
`probar_tokens` y `medir_runtime` apuntaban a `:8000`; `conversacion_voz`, `probar_ws` y
`probar_audio_http` a `:8100`. Los dos números fueron ciertos en su momento —el servidor
se levantó en los dos puertos en distintos días— y cada archivo se quedó con el que
tenía delante.

Para quien sigue el README eso es una trampa cronometrada: `make up` levanta el `:8000`
que documenta el Makefile, las tres suites de voz salen a buscar un `:8100` que no
existe, y la compuerta G2 se juzga sobre justo eso —«siguiendo únicamente tu README, la
solución queda corriendo y accesible»—.

El puerto se decide en un orden explícito:

  1. `--url` en la línea de órdenes, que gana siempre.
  2. `CENTINELA_URL`, para apuntar a otra máquina entera.
  3. `CENTINELA_PUERTO`, para el caso corriente de mover solo el puerto.
  4. 8000, el que documentan el README y el Makefile.

El paso 3 no es un lujo: en esta máquina de desarrollo un relé ajeno —un servicio de
otro proyecto, en WSL— tenía tomado `127.0.0.1:8000` y devolvía `{"detail":"Not Found"}`
a todo. Sin una forma de mover el puerto de una vez, había que pasar `--url` a mano en
cada suite, que es como los dos números se separaron en primer lugar.
"""

from __future__ import annotations

import os

PUERTO_DOCUMENTADO = 8000


def base() -> str:
    """El servidor al que apuntan los arneses, sin esquema ni barra final."""

    url = os.environ.get("CENTINELA_URL", "").strip()
    if url:
        destino = url.rstrip("/")
        for esquema in ("http://", "https://", "ws://", "wss://"):
            if destino.startswith(esquema):
                destino = destino[len(esquema):]
    else:
        puerto = os.environ.get("CENTINELA_PUERTO", str(PUERTO_DOCUMENTADO)).strip()
        destino = f"127.0.0.1:{puerto or PUERTO_DOCUMENTADO}"
    return destino


def url_http() -> str:
    return f"http://{base()}"


def url_ws() -> str:
    return f"ws://{base()}"
