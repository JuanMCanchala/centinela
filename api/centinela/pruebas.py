"""Ejecuta las suites de evaluacion del repo desde el panel, como subprocesos.

La decision de diseno que importa: **el panel no reimplementa ninguna
comprobacion**. Cada tarjeta de la consola de pruebas lanza exactamente el mismo
comando que documenta el README, y el veredicto es el codigo de salida de ese
comando. Nada mas.

Es tentador calcular la matriz de confusion dentro de la API para pintarla mas
bonito. Seria un error: habria dos implementaciones de la misma verdad, y en
cuanto divergieran, el numero que ve el jurado en pantalla y el numero que
reporta el informe dejarian de ser el mismo numero, sin que nadie se enterara.
Asi, si el panel dice 152/160, es porque `python -m eval.replay_triage` acaba de
imprimir 152/160 en esta maquina.

Consecuencia util: la consola tambien sirve como prueba de reproducibilidad. El
jurado puede correr las suites sin tocar la terminal.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]

# Tope de salida que guardamos en memoria por suite. pytest y humo son verbosos,
# y esto se sirve por JSON a un navegador.
MAX_SALIDA = 120_000

# Ni humo ni redteam son rapidas: hablan con la API de verdad, que a su vez
# invoca al modelo local en cada turno. Los tiempos que se ven en una maquina con
# la GPU ocupada son de varios minutos.
TIMEOUT_S = 900


@dataclass
class Suite:
    id: str
    titulo: str
    que: str
    argumentos: list[str]
    # Si es True, se le pasa --url apuntando a esta misma API: la suite hace
    # peticiones HTTP reales contra el servidor que la lanzo.
    necesita_url: bool = False


SUITES: tuple[Suite, ...] = (
    Suite(
        id="motor",
        titulo="Motor de decisión",
        que="Replay de los 160 casos oficiales del reto contra el motor de reglas. "
            "Reporta matriz de confusión y, sobre todo, falsos negativos clínicos.",
        argumentos=["-m", "eval.replay_triage"],
    ),
    Suite(
        id="unitarias",
        titulo="Pruebas unitarias",
        que="Guardarraíles, orden de la política, normalización con contexto, "
            "respuestas cortas y números hablados.",
        argumentos=["-m", "pytest", "tests", "-q"],
    ),
    Suite(
        id="adversarial",
        titulo="Suite adversarial",
        que="Intentos de manipular la criticidad, salirse de la misión, audio "
            "degradado, terceros que responden y jerga regional.",
        argumentos=["-m", "eval.redteam"],
        necesita_url=True,
    ),
    Suite(
        id="humo",
        titulo="Humo de extremo a extremo",
        que="Recorre la API completa: salud, corpus, llamada, turnos, cierre, "
            "ticket y recibo de olvido.",
        argumentos=["-m", "eval.humo"],
        necesita_url=True,
    ),
)

POR_ID = {s.id: s for s in SUITES}


@dataclass
class Ejecucion:
    """Estado de una corrida. `estado` es la maquina de estados completa."""

    estado: str = "pendiente"        # pendiente | corriendo | ok | fallo | error
    codigo_salida: int | None = None
    salida: str = ""
    iniciada_en: float | None = None
    ms: int | None = None
    comando: str = ""
    tarea: asyncio.Task | None = field(default=None, repr=False)

    def como_json(self) -> dict:
        return {
            "estado": self.estado,
            "codigo_salida": self.codigo_salida,
            "salida": self.salida,
            "ms": self.ms,
            "comando": self.comando,
        }


class CorredorPruebas:
    # `suites` es inyectable solo para poder probar este modulo con comandos
    # triviales: correr las suites de verdad dentro de una prueba unitaria
    # tardaria minutos y necesitaria el servidor levantado.
    def __init__(self, suites: tuple[Suite, ...] = SUITES) -> None:
        self._suites = suites
        self._por_id = {s.id: s for s in suites}
        self._ejecuciones: dict[str, Ejecucion] = {s.id: Ejecucion() for s in suites}

    def catalogo(self) -> list[dict]:
        salida = []
        for suite in self._suites:
            ejecucion = self._ejecuciones[suite.id]
            salida.append(
                {
                    "id": suite.id,
                    "titulo": suite.titulo,
                    "que": suite.que,
                    "comando_visible": self._comando_visible(suite),
                    **ejecucion.como_json(),
                }
            )
        return salida

    def estado(self, suite_id: str) -> dict:
        return self._ejecuciones[suite_id].como_json()

    @staticmethod
    def _comando_visible(suite: Suite) -> str:
        """El comando tal y como lo escribiria una persona en su terminal.

        Se muestra en la tarjeta a proposito: quien no confie en el panel puede
        copiarlo y obtener el mismo resultado.
        """

        partes = ["python", *suite.argumentos]
        if suite.necesita_url:
            partes += ["--url", "<esta API>"]
        return " ".join(partes)

    def lanzar(self, suite_id: str, url_base: str) -> dict:
        suite = self._por_id[suite_id]
        ejecucion = self._ejecuciones[suite_id]

        if ejecucion.estado == "corriendo":
            resultado = ejecucion.como_json()
        else:
            argumentos = list(suite.argumentos)
            if suite.necesita_url:
                argumentos += ["--url", url_base.rstrip("/")]

            nueva = Ejecucion(
                estado="corriendo",
                iniciada_en=time.monotonic(),
                comando=" ".join(["python", *argumentos]),
                salida="",
            )
            self._ejecuciones[suite_id] = nueva
            nueva.tarea = asyncio.create_task(self._correr(suite_id, argumentos))
            resultado = nueva.como_json()

        return resultado

    async def _correr(self, suite_id: str, argumentos: list[str]) -> None:
        ejecucion = self._ejecuciones[suite_id]
        try:
            # sys.executable y no "python": la API corre dentro del venv del
            # proyecto y el `python` del PATH en Windows suele ser otro
            # interprete, sin faster-whisper ni el resto de dependencias.
            proceso = await asyncio.create_subprocess_exec(
                sys.executable,
                *argumentos,
                cwd=str(RAIZ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            ejecucion.estado = "error"
            ejecucion.salida = f"No se pudo lanzar el proceso: {exc}"
            ejecucion.ms = 0
        else:
            await self._acompanar(ejecucion, proceso)

    async def _acompanar(self, ejecucion: Ejecucion, proceso) -> None:
        """Va acumulando la salida mientras el proceso corre.

        Se lee linea a linea en vez de esperar a `communicate()` para que el
        panel pueda mostrar el avance de una suite que tarda minutos, en vez de
        un spinner opaco.
        """

        trozos: list[str] = []
        largo = 0
        truncado = False

        async def bombear() -> None:
            nonlocal largo, truncado
            async for cruda in proceso.stdout:
                linea = cruda.decode("utf-8", errors="replace")
                if largo < MAX_SALIDA:
                    trozos.append(linea)
                    largo += len(linea)
                    ejecucion.salida = "".join(trozos)
                elif not truncado:
                    truncado = True
                    ejecucion.salida += (
                        f"\n[salida truncada a {MAX_SALIDA:,d} caracteres; "
                        f"el proceso sigue corriendo]\n"
                    )

        try:
            await asyncio.wait_for(bombear(), timeout=TIMEOUT_S)
            codigo = await asyncio.wait_for(proceso.wait(), timeout=30)
        except asyncio.TimeoutError:
            proceso.kill()
            ejecucion.estado = "error"
            ejecucion.salida += f"\n[cortado por tiempo tras {TIMEOUT_S} s]\n"
            codigo = None
        else:
            ejecucion.codigo_salida = codigo
            # El veredicto es el codigo de salida y nada mas. No se interpreta la
            # salida de texto: si el comando dice que fallo, fallo.
            ejecucion.estado = "ok" if codigo == 0 else "fallo"

        if ejecucion.iniciada_en is not None:
            ejecucion.ms = int((time.monotonic() - ejecucion.iniciada_en) * 1000)
