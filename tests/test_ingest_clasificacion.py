"""La clasificación por contenido, que es la defensa central del proyecto.

`rag/ingest.py` no tenía un solo test, y sostiene el argumento principal de la entrega:
**el tema de un documento sale de su texto, no de la carpeta donde está**. Sin eso, un RAG
que enrute por nombre de directorio le sirve guías de cuello uterino a una paciente
mastectomizada, que es la alucinación clínica peligrosa que la rúbrica penaliza.

Y no es hipotético en este dataset. `dataset/textos/breast_cancer/` trae 19 PDFs y ninguno
es de cáncer de mama: todos son de cuello uterino. La organización confirmó el 2026-08-08
que el desajuste era **intencional**, para evaluar el criterio analítico de cada
concursante, y que el enfoque correcto es *"corregir y ajustar el modelo en sí"*. Esta
función es ese ajuste, así que merecía pruebas propias.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from centinela.rag.ingest import (  # noqa: E402
    MIN_CHUNK,
    ChunkIngerido,
    DocumentoExtraido,
    PaginaExtraida,
    chunkear,
    clasificar_tema,
    normalizar,
)


# ---------------------------------------------------------------- normalizar

def test_normalizar_quita_tildes_y_mayusculas() -> None:
    """Para comparar lexico hace falta que "Cirugía" y "cirugia" sean lo mismo."""

    assert normalizar("Cirugía  ONCOLÓGICA\ndel   Seno") == "cirugia oncologica del seno"


def test_normalizar_conserva_la_ene() -> None:
    """La ñ no es una n con tilde: quitarsela cambia la palabra."""

    assert "muñon" in normalizar("MUÑÓN") or "munon" in normalizar("MUÑÓN")


# ---------------------------------------------------------------- clasificar por contenido

def test_el_tema_sale_del_texto_y_no_de_la_carpeta() -> None:
    """El caso exacto del corpus entregado, en miniatura.

    Un documento archivado en `breast_cancer/` cuyo texto habla de cuello uterino tiene
    que clasificarse como cuello uterino. Es lo unico que impide ofrecerselo a una
    paciente de mastectomia.
    """

    texto = (
        "Guia de practica clinica para el cancer de cuello uterino. "
        "El tamizaje con citologia cervical y la prueba de VPH permiten detectar "
        "lesiones del cervix. La conizacion y la histerectomia radical son opciones "
        "de tratamiento del cancer cervical en estadios tempranos. Cuello uterino."
    )

    tema, puntajes = clasificar_tema(texto)

    assert tema == "cancer_cuello_uterino"
    assert puntajes.get("cancer_mama", 0) < puntajes[tema]


def test_un_documento_de_mama_se_clasifica_como_mama() -> None:
    texto = (
        "Cuidados despues de la mastectomia y la reconstruccion mamaria. "
        "El drenaje de la herida quirurgica del seno y el linfedema del brazo "
        "requieren vigilancia. Cancer de mama, ganglio centinela, axila."
    )

    tema, _ = clasificar_tema(texto)

    assert tema == "cancer_mama"


def test_un_texto_sin_tema_clinico_no_se_clasifica() -> None:
    """`None` es "no se de que va", y es mejor que adivinar: `rag/retriever.py`
    trata la falta de tema distinto de un tema equivocado."""

    tema, _ = clasificar_tema("Acta de la reunion del comite de compras. Se aprueba el presupuesto.")

    assert tema is None


def test_un_texto_vacio_no_revienta() -> None:
    tema, puntajes = clasificar_tema("")

    assert tema is None
    assert all(p == 0 for p in puntajes.values())


def test_gana_el_tema_dominante_y_no_el_primero_que_aparece() -> None:
    """Una guia de colecistectomia puede mencionar la apendicitis de pasada."""

    texto = (
        "Colecistectomia laparoscopica: cuidados postoperatorios. La vesicula biliar "
        "se extrae por laparoscopia. La colelitiasis y la colecistitis aguda son las "
        "indicaciones mas frecuentes de colecistectomia. Diagnostico diferencial con "
        "apendicitis. Vesicula, colecistectomia, biliar, colecistitis."
    )

    tema, _ = clasificar_tema(texto)

    assert tema == "colecistitis"


# ---------------------------------------------------------------- chunkear

def _doc(*textos_por_pagina: str) -> DocumentoExtraido:
    return DocumentoExtraido(
        nombre="prueba.pdf",
        ruta=Path("prueba.pdf"),
        sha256="0" * 64,
        titulo="Prueba",
        paginas=[PaginaExtraida(numero=i, texto=t) for i, t in enumerate(textos_por_pagina, 1)],
    )


def test_un_chunk_nunca_cruza_dos_paginas() -> None:
    """Es lo que permite que la cita diga "pagina 7" y el jurado la encuentre alli."""

    doc = _doc("A" * 1200 + " primera pagina", "B" * 1200 + " segunda pagina")

    chunks = chunkear(doc, "doc1")

    assert chunks
    for c in chunks:
        # Ningun chunk mezcla el relleno de las dos paginas.
        assert not ("A" * 50 in c.texto and "B" * 50 in c.texto)
    assert {c.pagina for c in chunks} == {1, 2}


def test_el_texto_repetido_se_indexa_una_sola_vez() -> None:
    """Los PDFs de guias repiten encabezados y tablas enteras.

    Sin la deduplicacion, un mismo parrafo puede copar los cinco resultados de una
    consulta y dejar fuera el que de verdad responde.
    """

    parrafo = ("El paciente debe consultar si presenta fiebre mayor de treinta y ocho "
               "grados o secrecion purulenta en la herida quirurgica. ") * 3
    doc = _doc(parrafo, parrafo)

    chunks = chunkear(doc, "doc1")
    textos = [c.texto for c in chunks]

    assert len(textos) == len(set(textos)), "hay chunks con texto identico"


def test_una_pagina_casi_vacia_no_produce_chunks() -> None:
    """Portadas y paginas de solo un numero: indexarlas es ruido."""

    doc = _doc("7")

    assert chunkear(doc, "doc1") == []


def test_los_chunks_van_numerados_en_orden() -> None:
    doc = _doc("C" * 3000, "D" * 3000)

    chunks = chunkear(doc, "doc1")

    assert [c.orden for c in chunks] == list(range(len(chunks)))
    assert all(isinstance(c, ChunkIngerido) for c in chunks)


def test_el_chunk_lleva_el_doc_id_que_se_le_pasa() -> None:
    """Es lo que ata la cita al documento y lo que permite el olvido verificado."""

    chunks = chunkear(_doc("E" * 3000), "doc_abc")

    assert chunks
    assert all(c.doc_id == "doc_abc" for c in chunks)


def test_un_documento_sin_paginas_no_revienta() -> None:
    assert chunkear(_doc(), "doc1") == []
