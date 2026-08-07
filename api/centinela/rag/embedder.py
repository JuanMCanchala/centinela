"""Embeddings multilingues por ONNX Runtime, sin PyTorch.

Eleccion de modelo: `intfloat/multilingual-e5-large`.

El `stack-tecnico.md` del reto sugiere BGE-M3. Lo evaluamos y lo descartamos por
una razon de ingenieria, no de calidad: BGE-M3 no esta en el catalogo de
`fastembed`, asi que usarlo obliga a traer `sentence-transformers` y con el
PyTorch completo (~2.5 GB adicionales en la imagen). La compuerta G2 del reto da
15 minutos para levantar la solucion, y multilingual-e5-large ofrece
recuperacion multilingue comparable corriendo sobre ONNX Runtime puro.

Detalle que rompe el modelo si se omite: la familia E5 esta entrenada con
prefijos asimetricos. Una consulta se embebe como "query: ..." y un pasaje como
"passage: ...". Mezclarlos degrada la recuperacion de forma silenciosa, asi que
esta clase es la unica que toca los prefijos y nadie mas los escribe a mano.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Iterable, Sequence

MODELO_POR_DEFECTO = "intfloat/multilingual-e5-large"
DIM_POR_DEFECTO = 1024


class Embedder:
    def __init__(self, nombre_modelo: str | None = None, hilos: int | None = None) -> None:
        self.nombre_modelo = nombre_modelo or os.environ.get(
            "CENTINELA_EMBED_MODEL", MODELO_POR_DEFECTO
        )
        self.hilos = hilos or int(os.environ.get("CENTINELA_EMBED_THREADS", "0")) or None
        self._modelo = None

    @property
    def modelo(self):
        if self._modelo is None:
            from fastembed import TextEmbedding

            kwargs = {"model_name": self.nombre_modelo}
            if self.hilos:
                kwargs["threads"] = self.hilos
            self._modelo = TextEmbedding(**kwargs)
        return self._modelo

    @property
    def dim(self) -> int:
        return DIM_POR_DEFECTO if "e5-large" in self.nombre_modelo else 768

    def embed_pasajes(self, textos: Sequence[str]) -> list[list[float]]:
        prefijados = [f"passage: {t}" for t in textos]
        vectores = [v.tolist() for v in self.modelo.embed(prefijados)]
        return vectores

    def embed_consulta(self, texto: str) -> list[float]:
        vector = next(iter(self.modelo.embed([f"query: {texto}"]))).tolist()
        return vector

    def calentar(self) -> None:
        """Primera inferencia fuera del camino critico de la llamada."""

        self.embed_consulta("calentamiento")


@lru_cache(maxsize=1)
def embedder_compartido() -> Embedder:
    return Embedder()
