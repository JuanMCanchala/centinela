"""Almacen de conocimiento clinico con olvido demostrable.

La compuerta G5 del reto dice: "subes un documento desde tu consola y el agente
lo usa; lo eliminas y el agente lo olvida". La parte dificil no es agregar: es
demostrar que el borrado fue real y no un filtro cosmetico encima de un indice
que sigue conteniendo el texto.

Como se garantiza aqui:

1. **Chunks direccionados por contenido.** El id de un chunk es el sha256 de su
   texto mas el documento del que salio. Dos ingestas del mismo documento
   producen exactamente los mismos ids, asi que no hay duplicados silenciosos.

2. **Borrado fisico, no logico.** `eliminar_documento` borra los vectores de
   Chroma y las filas de SQLite. El documento queda en `auditoria` como
   tombstone -- el registro de que existio y se elimino -- pero su texto ya no
   esta en ninguna estructura consultable.

3. **Contador de generacion.** Cualquier mutacion incrementa `generacion`. El
   indice lexico BM25 y cualquier cache se reconstruyen cuando la generacion
   cambia, asi que es imposible responder con conocimiento de una version
   anterior del corpus.

4. **Recibo de olvido.** `recibo_de_olvido` corre la misma consulta antes y
   despues de un borrado y guarda ambas listas de citas. Es la evidencia que se
   le muestra al jurado: la cita estaba, ya no esta, y quedo registrado quien la
   borro y cuando.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

ESQUEMA = """
CREATE TABLE IF NOT EXISTS documentos (
    doc_id       TEXT PRIMARY KEY,
    nombre       TEXT NOT NULL,
    titulo       TEXT,
    sha256       TEXT NOT NULL,
    huella_texto TEXT,
    origen       TEXT NOT NULL,
    categoria    TEXT,
    tema         TEXT,
    n_paginas    INTEGER NOT NULL DEFAULT 0,
    n_chunks     INTEGER NOT NULL DEFAULT 0,
    paginas_ocr  INTEGER NOT NULL DEFAULT 0,
    ingerido_en  TEXT NOT NULL,
    generacion   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id  TEXT PRIMARY KEY,
    doc_id    TEXT NOT NULL REFERENCES documentos(doc_id) ON DELETE CASCADE,
    orden     INTEGER NOT NULL,
    pagina    INTEGER NOT NULL,
    texto     TEXT NOT NULL,
    n_tokens  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

CREATE TABLE IF NOT EXISTS auditoria (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    momento     TEXT NOT NULL,
    accion      TEXT NOT NULL,
    doc_id      TEXT,
    nombre      TEXT,
    detalle     TEXT,
    generacion  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    clave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recibos_olvido (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    momento        TEXT NOT NULL,
    doc_id         TEXT NOT NULL,
    nombre         TEXT NOT NULL,
    consulta       TEXT NOT NULL,
    citas_antes    TEXT NOT NULL,
    citas_despues  TEXT NOT NULL,
    olvido_probado INTEGER NOT NULL
);
"""


def sha256_texto(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def sha256_bytes(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


@dataclass
class ChunkIngerido:
    chunk_id: str
    doc_id: str
    orden: int
    pagina: int
    texto: str
    n_tokens: int


@dataclass
class DocumentoRegistrado:
    doc_id: str
    nombre: str
    titulo: str | None
    sha256: str
    origen: str
    categoria: str | None
    tema: str | None
    n_paginas: int
    n_chunks: int
    paginas_ocr: int
    ingerido_en: str
    generacion: int


class KnowledgeStore:
    """SQLite para el rastro auditable, Chroma para los vectores.

    Se mantienen deliberadamente separados: SQLite es la fuente de verdad de que
    documentos existen y que paso con ellos; Chroma es solo un indice. Si Chroma
    se corrompe, se reconstruye desde SQLite. Al contrario no.
    """

    COLECCION = "corpus_clinico"

    def __init__(self, ruta_datos: Path, dim_embeddings: int = 384) -> None:
        self.ruta = Path(ruta_datos)
        self.ruta.mkdir(parents=True, exist_ok=True)
        self.ruta_sqlite = self.ruta / "centinela.db"
        self.ruta_chroma = self.ruta / "chroma"
        self.dim_embeddings = dim_embeddings
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.ruta_sqlite, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(ESQUEMA)
        self._conn.commit()
        self._coleccion = None

    # ------------------------------------------------------------------
    # Chroma perezoso: no se toca hasta que hace falta, para que arrancar
    # la API no dependa de cargar el indice vectorial.
    # ------------------------------------------------------------------

    @property
    def coleccion(self):
        if self._coleccion is None:
            import chromadb
            from chromadb.config import Settings

            cliente = chromadb.PersistentClient(
                path=str(self.ruta_chroma),
                settings=Settings(anonymized_telemetry=False, allow_reset=True),
            )
            self._coleccion = cliente.get_or_create_collection(
                name=self.COLECCION,
                metadata={"hnsw:space": "cosine"},
            )
        return self._coleccion

    # ------------------------------------------------------------------
    # Generacion
    # ------------------------------------------------------------------

    @property
    def generacion(self) -> int:
        fila = self._conn.execute("SELECT valor FROM meta WHERE clave='generacion'").fetchone()
        valor = int(fila["valor"]) if fila else 0
        return valor

    def _bump_generacion(self) -> int:
        nueva = self.generacion + 1
        self._conn.execute(
            "INSERT INTO meta(clave, valor) VALUES('generacion', ?) "
            "ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor",
            (str(nueva),),
        )
        self._conn.commit()
        return nueva

    # ------------------------------------------------------------------
    # Escritura
    # ------------------------------------------------------------------

    def existe_contenido(self, huella_texto: str) -> DocumentoRegistrado | None:
        """Dedup logico: mismo contenido con otro nombre de archivo.

        El corpus del reto trae `Recommendations for follow-up of colorectal
        cancer survivors.pdf` y `ecommendations for follow-up...pdf`: el mismo
        articulo (DOI 10.1007/s12094-019-02059-1) con bytes distintos, asi que
        ningun hash de archivo los detecta. La huella se calcula sobre el texto
        normalizado del documento, no sobre sus bytes.
        """

        fila = self._conn.execute(
            "SELECT * FROM documentos WHERE huella_texto = ?", (huella_texto,)
        ).fetchone()
        resultado = self._a_documento(fila) if fila else None
        return resultado

    def registrar_documento(
        self,
        nombre: str,
        titulo: str | None,
        sha256: str,
        huella_texto: str,
        origen: str,
        categoria: str | None,
        tema: str | None,
        n_paginas: int,
        paginas_ocr: int,
        chunks: Sequence[ChunkIngerido],
        embeddings: Sequence[Sequence[float]],
    ) -> DocumentoRegistrado:
        with self._lock:
            doc_id = sha256[:32]
            generacion = self._bump_generacion()
            ahora = datetime.now(timezone.utc).isoformat()

            self._conn.execute("DELETE FROM documentos WHERE doc_id = ?", (doc_id,))
            self._conn.execute(
                "INSERT INTO documentos(doc_id, nombre, titulo, sha256, huella_texto, origen,"
                " categoria, tema, n_paginas, n_chunks, paginas_ocr, ingerido_en, generacion)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    doc_id, nombre, titulo, sha256, huella_texto, origen, categoria, tema,
                    n_paginas, len(chunks), paginas_ocr, ahora, generacion,
                ),
            )
            self._conn.executemany(
                "INSERT OR REPLACE INTO chunks(chunk_id, doc_id, orden, pagina, texto, n_tokens)"
                " VALUES(?,?,?,?,?,?)",
                [(c.chunk_id, doc_id, c.orden, c.pagina, c.texto, c.n_tokens) for c in chunks],
            )
            self._auditar(
                "ingesta", doc_id, nombre,
                {"n_chunks": len(chunks), "n_paginas": n_paginas, "paginas_ocr": paginas_ocr},
                generacion,
            )
            self._conn.commit()

            if chunks:
                self.coleccion.upsert(
                    ids=[c.chunk_id for c in chunks],
                    embeddings=[list(e) for e in embeddings],
                    documents=[c.texto for c in chunks],
                    metadatas=[
                        {
                            "doc_id": doc_id,
                            "nombre": nombre,
                            "pagina": c.pagina,
                            "orden": c.orden,
                            "categoria": categoria or "",
                            "tema": tema or "",
                        }
                        for c in chunks
                    ],
                )

            registrado = DocumentoRegistrado(
                doc_id=doc_id, nombre=nombre, titulo=titulo, sha256=sha256, origen=origen,
                categoria=categoria, tema=tema, n_paginas=n_paginas, n_chunks=len(chunks),
                paginas_ocr=paginas_ocr, ingerido_en=ahora, generacion=generacion,
            )
        return registrado

    def eliminar_documento(self, doc_id: str) -> dict:
        """Borrado fisico de vectores y texto. Deja tombstone en auditoria."""

        with self._lock:
            fila = self._conn.execute(
                "SELECT * FROM documentos WHERE doc_id = ?", (doc_id,)
            ).fetchone()

            if fila is None:
                resultado = {"eliminado": False, "razon": "documento inexistente"}
            else:
                nombre = fila["nombre"]
                n_chunks = fila["n_chunks"]
                ids_chunks = [
                    r["chunk_id"]
                    for r in self._conn.execute(
                        "SELECT chunk_id FROM chunks WHERE doc_id = ?", (doc_id,)
                    )
                ]
                if ids_chunks:
                    self.coleccion.delete(ids=ids_chunks)

                self._conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
                self._conn.execute("DELETE FROM documentos WHERE doc_id = ?", (doc_id,))
                generacion = self._bump_generacion()
                self._auditar(
                    "eliminacion", doc_id, nombre,
                    {"chunks_borrados": len(ids_chunks), "n_chunks_registrados": n_chunks},
                    generacion,
                )
                self._conn.commit()

                # Verificacion activa: preguntamos a Chroma si queda algo.
                restantes = self.coleccion.get(where={"doc_id": doc_id}, limit=1)
                quedan = len(restantes.get("ids") or [])

                resultado = {
                    "eliminado": True,
                    "doc_id": doc_id,
                    "nombre": nombre,
                    "chunks_borrados": len(ids_chunks),
                    "vectores_residuales": quedan,
                    "olvido_verificado": quedan == 0,
                    "generacion": generacion,
                }
        return resultado

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------

    def listar_documentos(self) -> list[DocumentoRegistrado]:
        filas = self._conn.execute(
            "SELECT * FROM documentos ORDER BY ingerido_en DESC"
        ).fetchall()
        docs = [self._a_documento(f) for f in filas]
        return docs

    def obtener_documento(self, doc_id: str) -> DocumentoRegistrado | None:
        fila = self._conn.execute(
            "SELECT * FROM documentos WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        doc = self._a_documento(fila) if fila else None
        return doc

    def chunks_activos(self) -> list[dict]:
        """Todos los chunks vigentes. Alimenta el indice BM25."""

        filas = self._conn.execute(
            "SELECT c.chunk_id, c.doc_id, c.pagina, c.texto, d.nombre, d.categoria, d.tema"
            " FROM chunks c JOIN documentos d ON d.doc_id = c.doc_id"
            " ORDER BY c.doc_id, c.orden"
        ).fetchall()
        salida = [dict(f) for f in filas]
        return salida

    def chunk(self, chunk_id: str) -> dict | None:
        fila = self._conn.execute(
            "SELECT c.*, d.nombre, d.sha256 FROM chunks c JOIN documentos d"
            " ON d.doc_id = c.doc_id WHERE c.chunk_id = ?",
            (chunk_id,),
        ).fetchone()
        salida = dict(fila) if fila else None
        return salida

    def estadisticas(self) -> dict:
        docs = self._conn.execute("SELECT COUNT(*) n FROM documentos").fetchone()["n"]
        chunks = self._conn.execute("SELECT COUNT(*) n FROM chunks").fetchone()["n"]
        temas = self._conn.execute(
            "SELECT tema, COUNT(*) n FROM documentos GROUP BY tema ORDER BY n DESC"
        ).fetchall()
        stats = {
            "documentos": docs,
            "chunks": chunks,
            "generacion": self.generacion,
            "por_tema": {r["tema"] or "sin_clasificar": r["n"] for r in temas},
        }
        return stats

    def auditoria(self, limite: int = 100) -> list[dict]:
        filas = self._conn.execute(
            "SELECT * FROM auditoria ORDER BY id DESC LIMIT ?", (limite,)
        ).fetchall()
        salida = [dict(f) for f in filas]
        return salida

    # ------------------------------------------------------------------
    # Recibo de olvido
    # ------------------------------------------------------------------

    def guardar_recibo_olvido(
        self,
        doc_id: str,
        nombre: str,
        consulta: str,
        citas_antes: list[dict],
        citas_despues: list[dict],
    ) -> dict:
        """Evidencia de que el borrado cambio lo que el agente puede responder."""

        probado = not any(c.get("doc_id") == doc_id for c in citas_despues)
        momento = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO recibos_olvido(momento, doc_id, nombre, consulta, citas_antes,"
            " citas_despues, olvido_probado) VALUES(?,?,?,?,?,?,?)",
            (
                momento, doc_id, nombre, consulta,
                json.dumps(citas_antes, ensure_ascii=False),
                json.dumps(citas_despues, ensure_ascii=False),
                1 if probado else 0,
            ),
        )
        self._conn.commit()
        recibo = {
            "momento": momento,
            "doc_id": doc_id,
            "nombre": nombre,
            "consulta": consulta,
            "citas_antes": citas_antes,
            "citas_despues": citas_despues,
            "olvido_probado": probado,
        }
        return recibo

    def recibos_olvido(self, limite: int = 50) -> list[dict]:
        filas = self._conn.execute(
            "SELECT * FROM recibos_olvido ORDER BY id DESC LIMIT ?", (limite,)
        ).fetchall()
        salida = []
        for f in filas:
            d = dict(f)
            d["citas_antes"] = json.loads(d["citas_antes"])
            d["citas_despues"] = json.loads(d["citas_despues"])
            d["olvido_probado"] = bool(d["olvido_probado"])
            salida.append(d)
        return salida

    # ------------------------------------------------------------------

    def _auditar(
        self, accion: str, doc_id: str | None, nombre: str | None,
        detalle: dict, generacion: int,
    ) -> None:
        self._conn.execute(
            "INSERT INTO auditoria(momento, accion, doc_id, nombre, detalle, generacion)"
            " VALUES(?,?,?,?,?,?)",
            (
                datetime.now(timezone.utc).isoformat(), accion, doc_id, nombre,
                json.dumps(detalle, ensure_ascii=False), generacion,
            ),
        )

    @staticmethod
    def _a_documento(fila: sqlite3.Row) -> DocumentoRegistrado:
        doc = DocumentoRegistrado(
            doc_id=fila["doc_id"], nombre=fila["nombre"], titulo=fila["titulo"],
            sha256=fila["sha256"], origen=fila["origen"], categoria=fila["categoria"],
            tema=fila["tema"], n_paginas=fila["n_paginas"], n_chunks=fila["n_chunks"],
            paginas_ocr=fila["paginas_ocr"], ingerido_en=fila["ingerido_en"],
            generacion=fila["generacion"],
        )
        return doc

    def cerrar(self) -> None:
        self._conn.close()
