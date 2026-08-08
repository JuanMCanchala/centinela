"""Recuperacion hibrida con compuerta de fundamentacion.

Por que hibrida y no solo densa: las consultas de este dominio mezclan dos
registros muy distintos. El paciente dice "me sale un liquido amarillo de la
herida" (parafrasis, la busca el vector) y la guia clinica dice "secrecion
purulenta" con un umbral de "38 C" (termino exacto y numero, los busca BM25).
Un recuperador solo-denso pierde los numeros; uno solo-lexico pierde la
parafrasis del paciente, que es justamente el caso de uso del reto.

Fusion por Reciprocal Rank Fusion: no requiere calibrar puntajes entre dos
sistemas con escalas incomparables, solo sus rankings.

Y lo que de verdad separa esto de un RAG de demo: la **compuerta de
fundamentacion**. Recuperar siempre devuelve algo -- el vecino mas cercano
existe aunque el corpus no tenga nada que ver con la pregunta. Si se responde
con eso, el agente suena seguro mientras alucina. La compuerta decide si lo
recuperado sostiene una afirmacion clinica, y si no, el agente lo dice y escala.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Sequence

from ..models import Cita
from .embedder import Embedder, embedder_compartido
from .ingest import TEMA_POR_PROCEDIMIENTO, normalizar
from .store import KnowledgeStore

K_RRF = 60
N_CANDIDATOS = 24
N_FINAL = 5
LAMBDA_MMR = 0.7

# Umbrales de la compuerta de fundamentacion.
# Calibrados en `eval/calibrar_grounding.py` sobre preguntas con respuesta
# conocida contra el corpus y preguntas deliberadamente fuera de cobertura.
MIN_SIMILITUD_COSENO = 0.82
MIN_SOLAPE_LEXICO = 0.12

# El listón que se le pone al material complementario cuando es el unico soporte. Sale
# de una medicion, no de una intuicion: sobre el folleto de ejercicios tras mastectomia,
# lo que el folleto NO cubre da entre 0.00 y 0.25 de solape lexico, y lo que si cubre da
# entre 0.50 y 0.75. El valor esta en medio de esas dos bandas. `make rag` lo comprueba
# sobre las 12 preguntas de mastectomia, y los otros cuatro procedimientos no lo tocan
# porque su soporte no es complementario.
MIN_SOLAPE_COMPLEMENTARIO = 0.38
MIN_PASAJES = 1


@dataclass
class Pasaje:
    chunk_id: str
    doc_id: str
    nombre: str
    pagina: int
    texto: str
    tema: str | None
    # Con que categoria se ingirio el documento. Interesa una en concreto:
    # `complementario` marca material que NO viene del corpus del reto, y eso tiene que
    # llegar hasta la cita para que la respuesta lo declare.
    categoria: str | None = None
    similitud: float = 0.0
    rango_denso: int | None = None
    rango_lexico: int | None = None
    puntaje_rrf: float = 0.0
    solape_lexico: float = 0.0

    def a_cita(self, sha256: str | None = None, cita_textual: str | None = None) -> Cita:
        return Cita(
            documento=self.nombre,
            documento_sha256=sha256,
            pagina=self.pagina,
            cita_textual=cita_textual or frase_mas_relevante(self.texto, ""),
            puntaje=round(self.similitud, 4),
        )


@dataclass
class ResultadoRecuperacion:
    consulta: str
    pasajes: list[Pasaje]
    fundamentado: bool
    razon: str
    generacion: int
    cobertura_procedimiento: bool = True
    tema_esperado: str | None = None
    tema_filtrado: str | None = None

    @property
    def citas(self) -> list[dict]:
        return [
            {
                "doc_id": p.doc_id,
                "documento": p.nombre,
                "pagina": p.pagina,
                "similitud": round(p.similitud, 4),
                "chunk_id": p.chunk_id,
            }
            for p in self.pasajes
        ]


class Retriever:
    def __init__(self, store: KnowledgeStore, embedder: Embedder | None = None) -> None:
        self.store = store
        self.embedder = embedder or embedder_compartido()
        self._generacion_bm25 = -1
        self._bm25 = None
        self._chunks_bm25: list[dict] = []

    # ------------------------------------------------------------------
    # Indice lexico, reconstruido cuando cambia la generacion del corpus.
    # Es el mecanismo que impide responder con conocimiento ya borrado.
    # ------------------------------------------------------------------

    def _asegurar_bm25(self) -> None:
        generacion = self.store.generacion
        if generacion != self._generacion_bm25:
            from rank_bm25 import BM25Okapi

            self._chunks_bm25 = self.store.chunks_activos()
            corpus = [tokenizar(c["texto"]) for c in self._chunks_bm25]
            self._bm25 = BM25Okapi(corpus) if corpus else None
            self._generacion_bm25 = generacion

    # ------------------------------------------------------------------

    def recuperar(
        self,
        consulta: str,
        procedimiento: str | None = None,
        n_final: int = N_FINAL,
    ) -> ResultadoRecuperacion:
        self._asegurar_bm25()
        generacion = self.store.generacion

        # Filtro por tema cuando se conoce el procedimiento del paciente.
        #
        # Sin este filtro la recuperacion cruza procedimientos y el resultado es
        # peligroso, no solo impreciso: en una prueba real, la pregunta "cuando
        # puedo volver a hacer ejercicio despues de la cirugia" para una paciente
        # de COLECISTECTOMIA devolvio como mejor pasaje una guia de cancer de
        # cuello uterino, y el modelo respondio con total seguridad citandola.
        # Esa es la alucinacion clinica por recuperacion que la rubrica penaliza.
        #
        # Se prefiere abstenerse a responder con material de otro procedimiento:
        # un "no lo se" cuesta un punto, una indicacion equivocada cuesta un
        # paciente.
        tema_esperado = TEMA_POR_PROCEDIMIENTO.get(procedimiento or "", None)
        cobertura = self._hay_cobertura(tema_esperado)
        tema_filtro = tema_esperado if (tema_esperado and cobertura) else None

        densos = self._buscar_denso(consulta, tema_filtro)
        lexicos = self._buscar_lexico(consulta, tema_filtro)
        fusionados = self._fusionar(densos, lexicos)
        seleccionados = self._mmr(consulta, fusionados, n_final)

        for p in seleccionados:
            p.solape_lexico = solape_lexico(consulta, p.texto)

        fundamentado, razon = self._evaluar_fundamentacion(
            seleccionados, cobertura, tema_esperado, tema_filtro
        )

        resultado = ResultadoRecuperacion(
            consulta=consulta,
            pasajes=seleccionados,
            fundamentado=fundamentado,
            razon=razon,
            generacion=generacion,
            cobertura_procedimiento=cobertura,
            tema_esperado=tema_esperado,
            tema_filtrado=tema_filtro,
        )
        return resultado

    # ------------------------------------------------------------------

    def _buscar_denso(self, consulta: str, tema: str | None = None) -> list[Pasaje]:
        vector = self.embedder.embed_consulta(consulta)
        filtro = {"tema": tema} if tema else None
        try:
            crudo = self.store.coleccion.query(
                query_embeddings=[vector],
                n_results=N_CANDIDATOS,
                where=filtro,
                include=["documents", "metadatas", "distances"],
            )
        except Exception:
            crudo = {"ids": [[]]}

        pasajes: list[Pasaje] = []
        ids = (crudo.get("ids") or [[]])[0]
        docs = (crudo.get("documents") or [[]])[0]
        metas = (crudo.get("metadatas") or [[]])[0]
        dists = (crudo.get("distances") or [[]])[0]

        for rango, (cid, texto, meta, dist) in enumerate(zip(ids, docs, metas, dists)):
            pasajes.append(
                Pasaje(
                    chunk_id=cid,
                    doc_id=str(meta.get("doc_id", "")),
                    nombre=str(meta.get("nombre", "")),
                    pagina=int(meta.get("pagina", 0)),
                    texto=texto or "",
                    tema=str(meta.get("tema") or "") or None,
                    categoria=str(meta.get("categoria") or "") or None,
                    similitud=1.0 - float(dist),
                    rango_denso=rango,
                )
            )
        return pasajes

    def _buscar_lexico(self, consulta: str, tema: str | None = None) -> list[Pasaje]:
        pasajes: list[Pasaje] = []
        if self._bm25 is not None:
            puntajes = self._bm25.get_scores(tokenizar(consulta))
            mejores = sorted(range(len(puntajes)), key=lambda i: puntajes[i], reverse=True)
            # El filtro de tema se aplica despues de puntuar: BM25 puntua sobre el
            # corpus completo y aqui se descarta lo que no corresponde al
            # procedimiento, manteniendo el ranking relativo de lo que queda.
            if tema:
                mejores = [i for i in mejores if self._chunks_bm25[i].get("tema") == tema]
            for rango, idx in enumerate(mejores[:N_CANDIDATOS]):
                if puntajes[idx] > 0:
                    c = self._chunks_bm25[idx]
                    pasajes.append(
                        Pasaje(
                            chunk_id=c["chunk_id"],
                            doc_id=c["doc_id"],
                            nombre=c["nombre"],
                            pagina=int(c["pagina"]),
                            texto=c["texto"],
                            tema=c.get("tema"),
                            categoria=c.get("categoria"),
                            rango_lexico=rango,
                        )
                    )
        return pasajes

    def _fusionar(self, densos: list[Pasaje], lexicos: list[Pasaje]) -> list[Pasaje]:
        por_id: dict[str, Pasaje] = {}

        for p in densos:
            por_id[p.chunk_id] = p

        for p in lexicos:
            if p.chunk_id in por_id:
                por_id[p.chunk_id].rango_lexico = p.rango_lexico
            else:
                por_id[p.chunk_id] = p

        for p in por_id.values():
            puntaje = 0.0
            if p.rango_denso is not None:
                puntaje += 1.0 / (K_RRF + p.rango_denso + 1)
            if p.rango_lexico is not None:
                puntaje += 1.0 / (K_RRF + p.rango_lexico + 1)
            p.puntaje_rrf = puntaje

        ordenados = sorted(por_id.values(), key=lambda p: p.puntaje_rrf, reverse=True)
        return ordenados

    def _mmr(self, consulta: str, candidatos: list[Pasaje], n: int) -> list[Pasaje]:
        """Diversifica: evita cinco citas del mismo parrafo del mismo PDF."""

        seleccionados: list[Pasaje] = []
        restantes = list(candidatos[:N_CANDIDATOS])

        while restantes and len(seleccionados) < n:
            mejor = None
            mejor_valor = -1e9
            for cand in restantes:
                redundancia = max(
                    (jaccard(cand.texto, s.texto) for s in seleccionados), default=0.0
                )
                valor = LAMBDA_MMR * cand.puntaje_rrf - (1 - LAMBDA_MMR) * redundancia
                if valor > mejor_valor:
                    mejor_valor = valor
                    mejor = cand
            seleccionados.append(mejor)
            restantes.remove(mejor)

        # La similitud coseno solo la conocemos del lado denso; si un pasaje
        # entro solo por BM25 se estima con solape lexico para que la compuerta
        # tenga siempre un numero con el que decidir.
        for p in seleccionados:
            if p.rango_denso is None:
                p.similitud = min(0.99, 0.6 + solape_lexico(consulta, p.texto))

        return seleccionados

    # ------------------------------------------------------------------
    # Compuerta de fundamentacion
    # ------------------------------------------------------------------

    def _hay_cobertura(self, tema_esperado: str | None) -> bool:
        if tema_esperado is None:
            cobertura = True
        else:
            stats = self.store.estadisticas()
            cobertura = stats["por_tema"].get(tema_esperado, 0) > 0
        return cobertura

    def _evaluar_fundamentacion(
        self,
        pasajes: list[Pasaje],
        cobertura: bool,
        tema_esperado: str | None,
        tema_filtrado: str | None = None,
    ) -> tuple[bool, str]:
        if not pasajes:
            veredicto = (
                False,
                "El corpus no devolvio ningun pasaje para esta consulta"
                + (f" dentro del tema {tema_filtrado}." if tema_filtrado else "."),
            )
        elif not cobertura:
            veredicto = (
                False,
                f"El corpus cargado no cubre el procedimiento del paciente "
                f"(tema requerido: {tema_esperado}). Responder con otro tema seria "
                f"clinicamente inseguro.",
            )
        else:
            mejor = max(pasajes, key=lambda p: p.similitud)
            solape = max(p.solape_lexico for p in pasajes)
            suficiente_semantica = mejor.similitud >= MIN_SIMILITUD_COSENO
            suficiente_lexico = solape >= MIN_SOLAPE_LEXICO

            # Cuando TODO el soporte es material complementario, se exigen las dos
            # señales en vez de cualquiera de las dos. El `or` es correcto para el
            # corpus oficial, que cubre el tema entero: ahi una pregunta parafraseada
            # puede tener poco solape lexico y estar bien respondida.
            #
            # Con un folleto complementario no. Medido sobre el de ejercicios tras
            # mastectomia (23 fragmentos), la similitud no discrimina nada -- las siete
            # preguntas de prueba caen entre 0.843 y 0.883, todas por encima del 0.82 --
            # mientras el solape lexico separa limpio: 0.00 a 0.25 lo que el folleto no
            # cubre, 0.50 a 0.75 lo que si. Con el `or`, la similitud daba luz verde
            # siempre y la señal util no llegaba a contar: a "que hago si me sale
            # liquido de la herida" el agente contestaba "bombee con el puño 10 veces",
            # que es un ejercicio de linfedema.
            solo_complementario = all(p.categoria == "complementario" for p in pasajes)

            basta_el_complemento = (
                suficiente_semantica and solape >= MIN_SOLAPE_COMPLEMENTARIO
            )

            if solo_complementario and not basta_el_complemento:
                veredicto = (
                    False,
                    f"El unico soporte es material complementario y no responde esta "
                    f"pregunta: similitud {mejor.similitud:.3f} (minimo "
                    f"{MIN_SIMILITUD_COSENO}) y solape lexico {solape:.3f} (minimo "
                    f"{MIN_SOLAPE_COMPLEMENTARIO}), y a una fuente de alcance estrecho "
                    f"se le exigen las dos.",
                )
            elif suficiente_semantica or suficiente_lexico:
                veredicto = (
                    True,
                    f"Fundamentado: similitud maxima {mejor.similitud:.3f}, "
                    f"solape lexico {solape:.3f}.",
                )
            else:
                veredicto = (
                    False,
                    f"Soporte insuficiente: similitud maxima {mejor.similitud:.3f} "
                    f"(minimo {MIN_SIMILITUD_COSENO}) y solape lexico "
                    f"{solape:.3f} (minimo {MIN_SOLAPE_LEXICO}).",
                )
        return veredicto


# --------------------------------------------------------------------------
# Utilidades lexicas
# --------------------------------------------------------------------------

VACIAS = frozenset(
    """de la que el en y a los se del las un por con no una su para es al lo como mas
    o pero sus le ha si porque esta entre cuando muy sin sobre tambien me hasta hay
    donde quien desde todo nos durante todos uno les ni contra otros ese eso ante
    ellos e esto mi antes algunos que unos yo otro otras otra tanto esa estos mucho
    quienes nada muchos cual poco ella estar estas algunas algo nosotros the of and
    to in a is for with on by are as at be this that from or an it""".split()
)


def tokenizar(texto: str) -> list[str]:
    palabras = re.findall(r"[a-z0-9]+", normalizar(texto))
    utiles = [p for p in palabras if len(p) > 2 and p not in VACIAS]
    return utiles


def solape_lexico(consulta: str, pasaje: str) -> float:
    """Fraccion de terminos utiles de la consulta presentes en el pasaje."""

    tc = set(tokenizar(consulta))
    if not tc:
        valor = 0.0
    else:
        tp = set(tokenizar(pasaje))
        valor = len(tc & tp) / len(tc)
    return valor


def jaccard(a: str, b: str) -> float:
    ta, tb = set(tokenizar(a)), set(tokenizar(b))
    union = ta | tb
    valor = len(ta & tb) / len(union) if union else 0.0
    return valor


def frase_mas_relevante(pasaje: str, consulta: str) -> str:
    """La frase concreta que se cita al paciente y que el jurado va a verificar.

    Una cita a nivel de chunk no resiste verificacion: el jurado abre el PDF y
    tiene que leer un parrafo entero buscando de que se hablaba. Una frase exacta
    se encuentra con Ctrl+F.
    """

    frases = [f.strip() for f in re.split(r"(?<=[.!?])\s+", pasaje) if len(f.strip()) > 40]
    if not frases:
        elegida = pasaje[:280].strip()
    elif not consulta:
        elegida = frases[0]
    else:
        tc = set(tokenizar(consulta))
        elegida = max(
            frases,
            key=lambda f: len(tc & set(tokenizar(f))) / (1 + math.log(1 + len(f) / 120)),
        )
    return elegida[:400]
