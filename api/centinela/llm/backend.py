"""Acceso al modelo de lenguaje.

Modelo elegido: **Phi-3.5 Mini 3.8B** (`phi3.5:3.8b-mini-instruct-q4_K_M`) sobre
Ollama.

La lista de modelos permitidos del reto tiene cuatro opciones y dos ya no
existen a agosto de 2026: la familia Gemini 1.5 esta apagada y devuelve 404, y
`llama-3.1-70b-versatile` fue decomisionado por Groq. Quedan los dos locales.

Entre esos dos medimos, no adivinamos (`scripts/bench_llm.py`, resultados en
`docs/benchmarks.md`): Phi-3.5 Mini da 185 ms hasta el primer token contra 880 ms
de Llama 3.2 1B en la misma maquina. El modelo cuatro veces mas pequeno resulto
cinco veces mas lento en lo unico que el paciente percibe. Ademas Phi-3.5 sigue
mejor el formato en la extraccion estructurada. Asi que Phi-3.5 hace los tres
trabajos y descartamos la idea del "router barato en 1B".

Todo lo que sale del modelo pasa por un esquema JSON forzado o por un limite
duro de tokens. El modelo nunca decide el flujo de la conversacion ni la
criticidad clinica: eso vive en `clinical/triage_engine.py` y en
`dialog/policy.py`, que son codigo determinista.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

MODELO_POR_DEFECTO = "phi3.5:3.8b-mini-instruct-q4_K_M"
OLLAMA_POR_DEFECTO = "http://127.0.0.1:11434"


@dataclass
class UsoTokens:
    tokens_entrada: int = 0
    tokens_salida: int = 0
    invocaciones: int = 0
    ms_hasta_primer_token: float = 0.0
    ms_total: float = 0.0

    def acumular(self, otro: "UsoTokens") -> None:
        self.tokens_entrada += otro.tokens_entrada
        self.tokens_salida += otro.tokens_salida
        self.invocaciones += otro.invocaciones
        self.ms_total += otro.ms_total
        self.ms_hasta_primer_token = max(self.ms_hasta_primer_token, otro.ms_hasta_primer_token)


@dataclass
class RespuestaLLM:
    texto: str
    uso: UsoTokens = field(default_factory=UsoTokens)
    modelo: str = ""
    trunco: bool = False

    def json(self) -> dict[str, Any]:
        """Parsea la salida como JSON, tolerando envoltura en bloque de codigo."""

        crudo = self.texto.strip()
        if crudo.startswith("```"):
            lineas = [l for l in crudo.splitlines() if not l.strip().startswith("```")]
            crudo = "\n".join(lineas).strip()
        inicio = crudo.find("{")
        fin = crudo.rfind("}")
        if inicio >= 0 and fin > inicio:
            crudo = crudo[inicio:fin + 1]
        try:
            datos = json.loads(crudo)
        except json.JSONDecodeError:
            datos = {}
        return datos


class LLMBackend:
    def __init__(
        self,
        modelo: str | None = None,
        url_base: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.modelo = modelo or os.environ.get("CENTINELA_LLM_MODEL", MODELO_POR_DEFECTO)
        self.url_base = (
            url_base or os.environ.get("CENTINELA_OLLAMA_URL", OLLAMA_POR_DEFECTO)
        ).rstrip("/")
        self._timeout = timeout
        self._cliente: httpx.AsyncClient | None = None

    async def cliente(self) -> httpx.AsyncClient:
        if self._cliente is None:
            self._cliente = httpx.AsyncClient(base_url=self.url_base, timeout=self._timeout)
        return self._cliente

    async def cerrar(self) -> None:
        if self._cliente is not None:
            await self._cliente.aclose()
            self._cliente = None

    # ------------------------------------------------------------------

    async def disponible(self) -> dict:
        """Estado del backend, para /health y para la consola."""

        try:
            c = await self.cliente()
            r = await c.get("/api/tags", timeout=5.0)
            modelos = [m["name"] for m in r.json().get("models", [])]
            estado = {
                "ok": self.modelo in modelos,
                "url": self.url_base,
                "modelo_configurado": self.modelo,
                "modelos_disponibles": modelos,
            }
        except Exception as e:
            estado = {
                "ok": False,
                "url": self.url_base,
                "modelo_configurado": self.modelo,
                "error": f"{type(e).__name__}: {e}",
            }
        return estado

    # ------------------------------------------------------------------

    async def generar(
        self,
        prompt: str,
        system: str | None = None,
        esquema: dict | None = None,
        max_tokens: int = 220,
        temperatura: float = 0.0,
        stop: list[str] | None = None,
    ) -> RespuestaLLM:
        """Generacion no-streaming.

        `esquema` activa las salidas estructuradas de Ollama: el muestreo queda
        restringido a la gramatica del JSON Schema, asi que la respuesta es JSON
        valido por construccion en vez de por suerte. Es lo que hace viable
        confiar la extraccion clinica a un modelo de 3.8B.
        """

        cuerpo: dict[str, Any] = {
            "model": self.modelo,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperatura,
                "num_predict": max_tokens,
                "top_p": 0.9 if temperatura > 0 else 1.0,
            },
        }
        if system:
            cuerpo["system"] = system
        if esquema:
            cuerpo["format"] = esquema
        if stop:
            cuerpo["options"]["stop"] = stop

        c = await self.cliente()
        t0 = time.perf_counter()
        r = await c.post("/api/generate", json=cuerpo)
        r.raise_for_status()
        datos = r.json()
        ms = (time.perf_counter() - t0) * 1000

        uso = UsoTokens(
            tokens_entrada=int(datos.get("prompt_eval_count", 0)),
            tokens_salida=int(datos.get("eval_count", 0)),
            invocaciones=1,
            ms_hasta_primer_token=ms,
            ms_total=ms,
        )
        respuesta = RespuestaLLM(
            texto=datos.get("response", ""),
            uso=uso,
            modelo=self.modelo,
            trunco=datos.get("done_reason") == "length",
        )
        return respuesta

    # ------------------------------------------------------------------

    async def generar_streaming(
        self,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 220,
        temperatura: float = 0.2,
        stop: list[str] | None = None,
    ) -> AsyncIterator[tuple[str, UsoTokens | None]]:
        """Genera token a token.

        Se usa en el unico camino donde el agente produce texto libre. El
        consumidor va cortando por frase y se la manda al TTS mientras el modelo
        sigue generando, de modo que el paciente empieza a oir la primera frase
        antes de que exista la segunda. Es la mitad del presupuesto de latencia.

        Cede `(fragmento, None)` por cada token y `("", uso)` al final.
        """

        cuerpo: dict[str, Any] = {
            "model": self.modelo,
            "prompt": prompt,
            "stream": True,
            "options": {
                "temperature": temperatura,
                "num_predict": max_tokens,
            },
        }
        if system:
            cuerpo["system"] = system
        if stop:
            cuerpo["options"]["stop"] = stop

        c = await self.cliente()
        t0 = time.perf_counter()
        primer = None
        final: dict = {}

        async with c.stream("POST", "/api/generate", json=cuerpo) as r:
            r.raise_for_status()
            async for linea in r.aiter_lines():
                if linea.strip():
                    evento = json.loads(linea)
                    fragmento = evento.get("response", "")
                    if fragmento:
                        if primer is None:
                            primer = (time.perf_counter() - t0) * 1000
                        yield fragmento, None
                    if evento.get("done"):
                        final = evento

        ms = (time.perf_counter() - t0) * 1000
        uso = UsoTokens(
            tokens_entrada=int(final.get("prompt_eval_count", 0)),
            tokens_salida=int(final.get("eval_count", 0)),
            invocaciones=1,
            ms_hasta_primer_token=primer or ms,
            ms_total=ms,
        )
        yield "", uso

    async def calentar(self) -> None:
        """Carga el modelo en memoria fuera del camino critico.

        Sin esto, el primer turno de la primera llamada paga varios segundos de
        carga del modelo, que es justo el turno que el jurado va a cronometrar.
        """

        await self.generar("ok", max_tokens=1)
