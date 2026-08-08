"""Cuanto del corpus alcanza el agente, y con que fidelidad.

El sistema tenia dos afirmaciones sobre el RAG sin numero detras: que la compuerta de
fundamentacion evita cruzar procedimientos, y que las verificaciones posteriores
evitan cifras inventadas. Las dos son ciertas y las dos estaban demostradas con un
caso cada una en `eval/humo.py`. Un caso no es una medicion.

Este arnes hace las preguntas que un paciente hace de verdad, cruzadas con los cinco
procedimientos del dataset, y mide cuatro cosas. Dos son informativas y dos son
criterio de fallo:

  informativas  tasa de respuesta fundamentada, y de que nivel salio
                (generada y verificada / cita literal del corpus / abstencion)

  CRITERIO      citas de un procedimiento que no es el del paciente  -> tiene que ser 0
  CRITERIO      cifras en la respuesta que no estan en el pasaje     -> tiene que ser 0

La distincion importa: una tasa de abstencion alta no es un fallo, es el corpus
diciendo que no cubre esa pregunta -- y para un paciente de mastectomia el corpus
entregado no la cubre, eso ya esta medido en `docs/informe-corpus.md`. Una cita de otro
procedimiento SI es un fallo, y del tipo que la rubrica anota textualmente en el acta.

Que el numero de abstenciones no sea criterio de fallo tambien evita el incentivo
equivocado: bajar los umbrales de fundamentacion mejoraria esa cifra y empeoraria lo
que importa.

    python -m eval.rag_cobertura [--url http://127.0.0.1:8000]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx

RAIZ = Path(__file__).resolve().parents[1]

# Tema del corpus que le corresponde a cada procedimiento. Es el mapa que usa
# `rag/retriever.py` para filtrar, y aqui sirve para lo contrario: detectar una cita
# que viene de donde no debe.
TEMA_ESPERADO = {
    "Apendicectomía": "apendicitis",
    "Colecistectomía": "colecistitis",
    "Colectomía": "cancer_colorrectal",
    "Reemplazo de cadera/rodilla": "artroplastia",
    # El corpus entregado no trae ni un documento de cancer de mama: los 19 PDFs de
    # `breast_cancer/` son de cuello uterino. Esta entrada existe para que quede
    # explicito que el hueco es del material, no del mapa.
    "Mastectomía": None,
}

# Las preguntas. Salen de lo que un paciente pregunta por telefono el dia 3 despues de
# una cirugia, no de lo que un corpus responde bien.
PREGUNTAS = (
    "¿Puedo ducharme o me tengo que esperar?",
    "¿Cuándo puedo volver a hacer ejercicio?",
    "¿Qué hago si me sale un líquido de la herida?",
    "¿Cuándo debo ir a urgencias?",
    "¿Puedo manejar carro?",
    "¿Cuánto tiempo voy a estar incapacitado?",
    "¿Qué puedo comer estos días?",
    "¿Es normal que me duela todavía?",
    "¿Cuándo me quitan los puntos?",
    "¿Puedo cargar a mi nieto?",
    "¿Qué temperatura ya es fiebre y tengo que llamar?",
    "¿Me puedo tomar algo para el dolor?",
)

RE_NUMERO = re.compile(r"\d+(?:[.,]\d+)?")

# El par (cifra, unidad) se compara con la misma normalizacion que usa el sistema.
# Se importa de ahi en vez de copiarla: dos implementaciones de la misma regla acaban
# divergiendo, y entonces el arnes deja de medir lo que el sistema promete.
sys.path.insert(0, str(RAIZ / "api"))
from centinela.rag.answerer import _cifras_con_unidad as _pares  # noqa: E402

# Cifras que aparecen en una respuesta hablada sin ser un dato clinico: enumeraciones
# y el "24" de "24 horas" cuando viene del propio texto. Solo se excluyen las que no
# pueden ser una dosis ni un umbral.
NUMEROS_NEUTROS = frozenset({"1", "2", "3"})


def medir(cli: httpx.Client, procedimiento: str, pregunta: str) -> dict:
    r = cli.get(
        "/api/preguntar",
        params={"q": pregunta, "procedimiento": procedimiento, "dia": 3},
    )
    r.raise_for_status()
    d = r.json()

    citas = d.get("citas") or []
    texto = d.get("respuesta") or ""

    # Cifras de la respuesta que no estan en el contexto que se le puso delante al
    # modelo. Se comprueba contra `contexto_usado`, el texto real de los pasajes, no
    # contra la frase citada: la frase es una sola oracion por pasaje, y usarla como
    # referencia inventa fallos que no existen. La primera version de este arnes lo
    # hacia asi y marcaba como alucinacion cualquier cifra del resto del pasaje.
    contexto = d.get("contexto_usado") or ""
    en_respuesta = set(RE_NUMERO.findall(texto)) - NUMEROS_NEUTROS
    sin_respaldo = sorted(en_respuesta - set(RE_NUMERO.findall(contexto)))

    # Y la comprobacion que de verdad muerde: la cifra CON su unidad. "15" puede estar
    # en el contexto como "15 puntos" de una escala y aparecer en la respuesta como
    # "15 dias". La cifra esta; el dato es invencion.
    mal_contextualizadas = sorted(
        f"{n} {u}" for n, u in _pares(texto) - _pares(contexto)
    )

    esperado = TEMA_ESPERADO[procedimiento]
    cruzadas = []
    for c in citas:
        tema = c.get("tema")
        if esperado is not None and tema is not None and tema != esperado:
            cruzadas.append({"documento": c.get("documento"), "tema": tema})

    # De donde sale el respaldo. El corpus entregado no cubre mastectomia, y ese hueco se
    # tapo con guia publica de autoridades nombradas marcada aparte
    # (`scripts/ingerir_complementario.py`). Contar las dos cosas juntas y publicar una
    # sola cifra seria presentar como cobertura del material del reto algo que no lo es.
    fuentes = {c.get("fuente") or "corpus_oficial" for c in citas}
    solo_oficial = bool(citas) and fuentes == {"corpus_oficial"}

    return {
        "procedimiento": procedimiento,
        "pregunta": pregunta,
        "fundamentado": bool(d.get("fundamentado")),
        "solo_corpus_oficial": solo_oficial,
        "fuentes": sorted(fuentes),
        "extractiva": bool(d.get("extractiva")),
        "n_citas": len(citas),
        "texto": texto,
        "cifras_sin_respaldo": sin_respaldo,
        "cifras_mal_contextualizadas": mal_contextualizadas,
        "citas_cruzadas": cruzadas,
        "razon": d.get("razon") or "",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--procedimiento", default="", help="mide solo uno")
    args = ap.parse_args()

    cli = httpx.Client(base_url=args.url, timeout=180.0)
    try:
        cli.get("/api/salud").raise_for_status()
    except Exception as e:  # noqa: BLE001
        print(f"la API no responde en {args.url}: {type(e).__name__}: {e}")
        return 2

    procedimientos = [args.procedimiento] if args.procedimiento else list(TEMA_ESPERADO)

    print("=" * 78)
    print("COBERTURA DEL CORPUS - preguntas reales de paciente por procedimiento")
    print("=" * 78)
    print()

    t0 = time.perf_counter()
    resultados: list[dict] = []

    for proc in procedimientos:
        print(f"{proc}")
        for pregunta in PREGUNTAS:
            r = medir(cli, proc, pregunta)
            resultados.append(r)
            if r["fundamentado"]:
                marca = "CITA" if r["extractiva"] else "RESP"
            else:
                marca = "  --"
            aviso = ""
            if r["citas_cruzadas"]:
                aviso = f"   CRUZADA: {r['citas_cruzadas']}"
            elif r["cifras_sin_respaldo"]:
                aviso = f"   CIFRA SIN RESPALDO: {r['cifras_sin_respaldo']}"
            elif r["cifras_mal_contextualizadas"]:
                aviso = f"   CIFRA CON OTRA UNIDAD: {r['cifras_mal_contextualizadas']}"
            print(f"  {marca}  {pregunta[:58]:58s}{aviso}")
        print()

    # ------------------------------------------------------------------
    n = len(resultados)
    fundamentadas = [r for r in resultados if r["fundamentado"]]
    con_oficial = [r for r in fundamentadas if r["solo_corpus_oficial"]]
    con_complemento = [r for r in fundamentadas if not r["solo_corpus_oficial"]]
    extractivas = [r for r in fundamentadas if r["extractiva"]]
    cruzadas = [r for r in resultados if r["citas_cruzadas"]]
    cifras = [r for r in resultados if r["cifras_sin_respaldo"]]
    unidades = [r for r in resultados if r["cifras_mal_contextualizadas"]]

    print("=" * 78)
    print("RESULTADO")
    print("=" * 78)
    print(f"  preguntas medidas              : {n} "
          f"({len(PREGUNTAS)} x {len(procedimientos)} procedimientos)")
    print(f"  respondidas con cita del corpus: {len(fundamentadas)} "
          f"({len(fundamentadas) / n:.0%})")
    print(f"     de esas, generadas y verificadas: {len(fundamentadas) - len(extractivas)}")
    print(f"     de esas, cita literal del corpus: {len(extractivas)}")
    print()
    print("  y por origen del respaldo, que es la cifra honesta:")
    print(f"     solo con el corpus OFICIAL del reto : {len(con_oficial)}/{n}")
    print(f"     apoyadas en material complementario : {len(con_complemento)}/{n}")
    print(f"  abstenciones                   : {n - len(fundamentadas)} "
          f"({(n - len(fundamentadas)) / n:.0%})")
    print()
    print(f"  CITAS DE OTRO PROCEDIMIENTO    : {len(cruzadas)}   <-- tiene que ser 0")
    print(f"  CIFRAS SIN RESPALDO EN EL CORPUS: {len(cifras)}   <-- tiene que ser 0")
    print(f"  CIFRAS CON OTRA UNIDAD         : {len(unidades)}   <-- tiene que ser 0")

    # Por procedimiento, porque el hueco de mastectomia tiene que verse.
    print()
    print("  por procedimiento:")
    for proc in procedimientos:
        suyas = [r for r in resultados if r["procedimiento"] == proc]
        ok = [r for r in suyas if r["fundamentado"]]
        oficial = [r for r in ok if r["solo_corpus_oficial"]]
        detalle = ""
        if len(oficial) != len(ok):
            detalle = f"   ({len(oficial)} con el corpus oficial, el resto con complemento)"
        print(f"    {proc:30s} {len(ok)}/{len(suyas)} respondidas{detalle}")

    destino = RAIZ / "docs" / "metrics" / "rag_cobertura.json"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(
        json.dumps(
            {
                "n_preguntas": n,
                "fundamentadas": len(fundamentadas),
                "fundamentadas_solo_corpus_oficial": len(con_oficial),
                "fundamentadas_con_complemento": len(con_complemento),
                "extractivas": len(extractivas),
                "abstenciones": n - len(fundamentadas),
                "citas_cruzadas": len(cruzadas),
                "cifras_sin_respaldo": len(cifras),
                "cifras_mal_contextualizadas": len(unidades),
                "por_procedimiento": {
                    proc: {
                        "respondidas": len([
                            r for r in resultados
                            if r["procedimiento"] == proc and r["fundamentado"]
                        ]),
                        "respondidas_solo_corpus_oficial": len([
                            r for r in resultados
                            if r["procedimiento"] == proc and r["fundamentado"]
                            and r["solo_corpus_oficial"]
                        ]),
                        "preguntas": len(PREGUNTAS),
                    }
                    for proc in procedimientos
                },
                "detalle": resultados,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print()
    print(f"  informe en {destino.relative_to(RAIZ)}")
    print(f"  {time.perf_counter() - t0:.1f}s")

    fallos = []
    if cruzadas:
        fallos.append(f"{len(cruzadas)} citas de un procedimiento distinto al del paciente")
    if cifras:
        fallos.append(f"{len(cifras)} respuestas con cifras que no estan en el corpus")
    if unidades:
        fallos.append(
            f"{len(unidades)} respuestas con una cifra del corpus usada con otra unidad"
        )

    if fallos:
        print()
        print("  FALLOS:")
        for f in fallos:
            print(f"    - {f}")
        for r in cruzadas + cifras + unidades:
            print(f"      [{r['procedimiento']}] {r['pregunta']}")
            print(f"        {r['texto'][:150]}")

    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main())
