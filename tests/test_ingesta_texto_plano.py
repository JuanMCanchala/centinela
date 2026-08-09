"""La compuerta G5 no dice PDF, y la ingesta solo aceptaba PDF.

Texto de la compuerta: «Subes un documento desde tu consola de administración y el agente
lo usa; lo eliminas y el agente lo olvida. **Se verifica con un documento de prueba que no
forma parte de ningún corpus entregado.**»

El corpus del reto son PDFs y la ingesta se escribió para eso. Pero quien fabrica un
documento de prueba con un dato inventado lo más probable es que escriba un `.txt`: es la
forma más rápida. Y el endpoint contestaba `400 solo se aceptan archivos PDF`.

El riesgo es asimétrico y por eso se cubre: fallar una compuerta significa que **la entrega
no se puntúa**, y el formato que se rechazaba es más fácil de leer que el que se aceptaba.
"""

from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))

from centinela.rag.ingest import (  # noqa: E402
    CARACTERES_POR_PAGINA_SINTETICA,
    EXTENSIONES_ACEPTADAS,
    EXTENSIONES_TEXTO,
    chunkear,
    extraer_documento,
)

TEXTO = (
    "Protocolo institucional Zafiro-7749 para seguimiento telefonico postoperatorio.\n\n"
    "Ante secrecion serosa clara en la herida quirurgica durante las primeras 72 horas se "
    "aplica compresion con gasa esteril durante quince minutos y se documenta el volumen "
    "aproximado en mililitros.\n\n"
    "Si la secrecion persiste mas de veinte minutos, se contacta al cirujano tratante. "
    "Este protocolo aplica exclusivamente a la institucion que lo emite.\n"
)


def test_el_pdf_sigue_siendo_un_formato_aceptado() -> None:
    """Lo nuevo se añade; no se cambia lo que ya funcionaba con el corpus del reto."""

    assert ".pdf" in EXTENSIONES_ACEPTADAS


def test_txt_y_md_se_aceptan() -> None:
    for ext in (".txt", ".md"):
        assert ext in EXTENSIONES_ACEPTADAS


def test_un_txt_se_extrae_con_su_texto(tmp_path: Path) -> None:
    ruta = tmp_path / "protocolo.txt"
    ruta.write_text(TEXTO, encoding="utf-8")

    doc = extraer_documento(ruta)

    assert "Zafiro-7749" in doc.texto_completo
    assert doc.paginas
    assert doc.sha256


def test_un_txt_produce_fragmentos_indexables(tmp_path: Path) -> None:
    """Sin fragmentos el endpoint devuelve 422 y la compuerta falla igual."""

    ruta = tmp_path / "protocolo.txt"
    ruta.write_text(TEXTO, encoding="utf-8")

    chunks = chunkear(extraer_documento(ruta), "doc1")

    assert chunks
    assert any("Zafiro-7749" in c.texto for c in chunks)


def test_un_texto_largo_no_cita_siempre_la_pagina_uno(tmp_path: Path) -> None:
    """Un texto plano no tiene páginas y la cita las necesita.

    Sin partirlo, un documento de veinte mil caracteres citaría «página 1» para cualquier
    frase, y la rúbrica exige que la referencia «resista una verificación contra la fuente
    real»: nadie puede verificar una página que abarca el documento entero.
    """

    largo = (TEXTO + "\n") * 12
    ruta = tmp_path / "largo.md"
    ruta.write_text(largo, encoding="utf-8")

    doc = extraer_documento(ruta)

    assert len(doc.paginas) > 1
    assert max(p.numero for p in doc.paginas) == len(doc.paginas)
    assert all(len(p.texto) <= CARACTERES_POR_PAGINA_SINTETICA + 1 for p in doc.paginas)


def test_las_paginas_sinteticas_no_parten_palabras(tmp_path: Path) -> None:
    """La cita textual tiene que poder buscarse en el original: una frase cortada a mitad
    de palabra no se encuentra."""

    largo = " ".join(f"palabra{i:05d}" for i in range(900))
    ruta = tmp_path / "seguido.txt"
    ruta.write_text(largo, encoding="utf-8")

    doc = extraer_documento(ruta)

    for pagina in doc.paginas:
        for palabra in pagina.texto.split():
            assert palabra.startswith("palabra"), f"palabra partida: {palabra!r}"


def test_un_acento_mal_codificado_no_tira_la_ingesta(tmp_path: Path) -> None:
    """Lo que se pierde es un carácter, no el documento."""

    ruta = tmp_path / "latin.txt"
    ruta.write_bytes(TEXTO.encode("utf-8") + b"\xff\xfe secrecion")

    doc = extraer_documento(ruta)

    assert "Zafiro-7749" in doc.texto_completo


def test_el_texto_plano_pasa_por_el_clasificador_de_tema(tmp_path: Path) -> None:
    """No que le salga un tema: que se le pregunte.

    El documento de prueba de arriba no nombra ninguno de los cinco procedimientos, así
    que **su tema correcto es ninguno**, y así se guarda. Lo que se comprueba es que la
    clasificación corre también en el camino de texto plano: sin ella el campo quedaría
    vacío por omisión y no por medición, y eso es otra cosa.
    """

    ruta = tmp_path / "protocolo.txt"
    ruta.write_text(TEXTO, encoding="utf-8")

    doc = extraer_documento(ruta)

    assert doc.puntajes_tema, "el clasificador no corrio sobre el texto plano"
    assert doc.tema_detectado is None


def test_un_texto_de_un_procedimiento_si_recibe_su_tema(tmp_path: Path) -> None:
    """Y cuando el documento sí habla de un procedimiento, el tema se detecta: es lo que
    mantiene en pie el filtro que impide citar el procedimiento equivocado."""

    ruta = tmp_path / "apendice.txt"
    ruta.write_text(
        "Cuidados tras la apendicectomia laparoscopica. La apendicitis aguda operada "
        "requiere vigilar la herida quirurgica del apendice durante las primeras "
        "setenta y dos horas. Tras la apendicectomia el paciente puede caminar. "
        "La apendicitis complicada y la apendicectomia abierta tienen otro curso.\n",
        encoding="utf-8",
    )

    assert extraer_documento(ruta).tema_detectado == "apendicitis"


def test_un_documento_sin_tema_no_queda_invisible_en_las_llamadas() -> None:
    """El agujero que habría dejado la compuerta G5 sin efecto.

    El filtro por tema excluía todo lo que no coincidiera, y un documento subido desde la
    consola cuyo texto no nombra ninguno de los cinco procedimientos no coincide con
    nada: quedaba invisible en cualquier llamada con paciente conocido, que son todas. El
    jurado lo habría subido, lo habría visto indexado, y el agente no lo habría usado.

    Lo que sigue excluido es lo que importa: un tema detectado DISTINTO.
    """

    from centinela.rag.ingest import CATEGORIA_CONSOLA

    fuente = (RAIZ / "api" / "centinela" / "rag" / "retriever.py").read_text(
        encoding="utf-8"
    )
    assert '{"categoria": CATEGORIA_CONSOLA}' in fuente
    assert 'get("categoria") == CATEGORIA_CONSOLA' in fuente

    # Y el servidor tiene que marcar con esa categoria lo que sube por la consola, o el
    # permiso de arriba no se lo aplica a nadie.
    servidor = (RAIZ / "api" / "centinela" / "main.py").read_text(encoding="utf-8")
    assert "categoria=CATEGORIA_CONSOLA" in servidor
    assert CATEGORIA_CONSOLA == "subido_por_consola"


def test_el_permiso_es_para_lo_que_subio_un_operador_no_para_todo_lo_sin_tema() -> None:
    """La version amplia del arreglo rompio una garantia publicada, en la misma corrida.

    Dejar entrar «cualquier documento sin tema» tambien dejaba entrar el PDF del corpus
    que el clasificador no supo etiquetar, y la abstencion de mastectomia paso de «no la
    tengo» a una respuesta fundamentada con cinco citas. Un documento que un operador
    acaba de subir mientras la llamada corre y un PDF que el clasificador no entendio no
    son la misma cosa.
    """

    fuente = (RAIZ / "api" / "centinela" / "rag" / "retriever.py").read_text(
        encoding="utf-8"
    )
    assert '{"tema": {"$in": [tema, SIN_TEMA]}}' not in fuente, (
        "volvio la regla amplia: cualquier documento sin tema entra en cualquier busqueda"
    )


def test_el_endpoint_nombra_los_formatos_que_si_acepta() -> None:
    """Un `400` que no dice qué hacer cuesta minutos de una sesión cronometrada."""

    fuente = (RAIZ / "api" / "centinela" / "main.py").read_text(encoding="utf-8")
    assert "Formatos admitidos: " in fuente
    assert "EXTENSIONES_ACEPTADAS" in fuente


def test_la_consola_ofrece_los_mismos_formatos_que_el_servidor() -> None:
    """Si el selector de archivos filtra solo PDF, el `.txt` del jurado no se puede ni
    elegir -- y el servidor aceptándolo no sirve de nada."""

    html = (RAIZ / "web" / "index.html").read_text(encoding="utf-8")
    for ext in EXTENSIONES_TEXTO[:2]:
        assert ext in html, f"la consola no ofrece {ext}"
