"""Las cifras que la rúbrica exige en §5 no pueden quedarse rancias en silencio.

El verificador de cifras existía y declaraba, por escrito, que las de latencia y consumo
quedaban fuera «porque ya vienen de `docs/metricas.md`, que se genera». El razonamiento
tenía un agujero del tamaño de la sección entera: `docs/metricas.md` se genera, pero el
README **las copia a mano**. Cuando alguien fue a mirar había **diez cifras falsas a la
vez**: la muestra decía 42 turnos y eran 51, el costo por llamada decía USD 0.002425 y
era 0.002598, y una frase remitía a «los 415.4 tokens/llamada de arriba» cuando la tabla
de arriba decía 462.2.

La rúbrica lo castiga por nombre: *"Métricas inconsistentes con los logs de la sesión.
Limita severamente la calificación de Repositorio, proceso y buenas prácticas."*

Estas pruebas cubren los dos modos de fallo, y el segundo es el que importa:

1. Que una cifra rancia se detecte y se corrija.
2. **Que el patrón siga encontrando la cifra.** Si alguien reescribe el párrafo, el
   patrón deja de coincidir, `re.finditer` devuelve cero coincidencias y la cifra deja
   de comprobarse *sin que nada falle*. Un verificador que aprueba porque ya no mira es
   peor que no tenerlo: da la confianza sin el respaldo.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "scripts"))

import verificar_cifras as vc  # noqa: E402


# ==========================================================================
# 1. El patrón sigue encontrando la cifra que dice comprobar
# ==========================================================================

DOCUMENTOS = {
    "README.md": (RAIZ / "README.md").read_text(encoding="utf-8"),
    "informe-final.md": (RAIZ / "docs" / "informe-final.md").read_text(encoding="utf-8"),
}


@pytest.mark.parametrize("entrada", vc.CIFRAS_DE_RUNTIME, ids=lambda e: e[0])
def test_cada_patron_encuentra_su_cifra_en_algun_documento(entrada) -> None:
    """El modo de fallo silencioso: reescribir el texto y perder la comprobación."""

    import re

    nombre, patron, _rutas = entrada
    hallados = sum(
        len(re.findall(patron, vc.enmascarar(texto))) for texto in DOCUMENTOS.values()
    )
    assert hallados > 0, (
        f"el patron de «{nombre}» ya no encuentra nada: o la cifra desaparecio de los "
        f"documentos, o el texto se reescribio y la comprobacion quedo muerta"
    )


def test_todas_las_rutas_existen_en_la_medicion() -> None:
    """Un campo renombrado en `runtime.json` deja la ruta apuntando a nada."""

    datos = vc.aplanar(
        json.loads(
            (RAIZ / "docs" / "metrics" / "runtime.json").read_text(encoding="utf-8")
        )
    )
    huerfanas = [
        ruta
        for _nombre, _patron, rutas in vc.CIFRAS_DE_RUNTIME
        for ruta in rutas
        if vc.hondo(datos, ruta) is None
    ]
    assert huerfanas == [], f"rutas que la medicion ya no trae: {huerfanas}"


# ==========================================================================
# 2. Una cifra rancia se detecta
# ==========================================================================

def test_una_cifra_rancia_no_cuadra() -> None:
    assert not vc.Comprobacion("x", 51, "42", "README.md").cuadra


def test_treinta_y_siete_y_treinta_y_siete_punto_cero_son_la_misma_cifra() -> None:
    """El JSON redondea a un decimal y el documento escribe el que le sale.

    Comparar el texto marcaba como rancia una cifra correcta.
    """

    assert vc.Comprobacion("x", 37.0, "37.0", "README.md").cuadra
    assert vc.Comprobacion("x", 37, "37.0", "README.md").cuadra


def test_una_cifra_que_la_medicion_no_trae_no_es_lo_mismo_que_estar_mal() -> None:
    sin_medir = vc.Comprobacion("x", None, "13", "README.md")
    assert not sin_medir.medible


# ==========================================================================
# 3. La corrección escribe el número bien
# ==========================================================================

def test_el_desglose_del_costo_no_se_escribe_en_notacion_cientifica() -> None:
    """`str(5e-05)` es cierto y es ilegible en un documento."""

    assert vc.formatear(5e-05) == "0.00005"


def test_un_entero_flotante_conserva_su_decimal() -> None:
    assert vc.formatear(37.0) == "37.0"
    assert vc.formatear(462.2) == "462.2"
    assert vc.formatear(51) == "51"


def test_enmascarar_no_mueve_nada_de_sitio() -> None:
    """Si la máscara cambia la longitud, la corrección escribe en el sitio equivocado."""

    texto = "el README decía «55 tests» y ahora dice 687 tests"
    tapado = vc.enmascarar(texto)
    assert len(tapado) == len(texto)
    assert "55" not in tapado
    assert "687" in tapado


def test_corregir_escribe_la_medicion_encima_de_la_cifra(tmp_path, monkeypatch) -> None:
    doc = tmp_path / "README.md"
    doc.write_text("Muestra: **42 turnos en 11 llamadas** completas.", encoding="utf-8")
    monkeypatch.setattr(vc, "RAIZ", tmp_path)

    malas = vc.comprobar_runtime(
        (("README.md", doc.read_text(encoding="utf-8")),),
        {"resumen": {"n_turnos": 51, "n_llamadas": 12}},
    )
    vc.corregir([c for c in malas if not c.cuadra])

    assert "**51 turnos en 12 llamadas**" in doc.read_text(encoding="utf-8")


def test_corregir_varias_cifras_de_la_misma_linea_no_se_pisan(tmp_path, monkeypatch):
    """Reemplazar de izquierda a derecha desplaza las posiciones siguientes.

    Con longitudes distintas —«9.7» pasa a «10.4»— la segunda cifra se escribiría
    corrida un caracter y el documento quedaría peor que antes.
    """

    doc = tmp_path / "README.md"
    doc.write_text(
        "**Costo estimado por llamada: USD 0.002425** (COP 9.7). Fin.", encoding="utf-8"
    )
    monkeypatch.setattr(vc, "RAIZ", tmp_path)

    malas = vc.comprobar_runtime(
        (("README.md", doc.read_text(encoding="utf-8")),),
        {"costo": {
            "costo_total_usd_por_llamada": 0.002598,
            "costo_total_cop_por_llamada": 10.4,
        }},
    )
    vc.corregir([c for c in malas if not c.cuadra])

    assert (
        doc.read_text(encoding="utf-8")
        == "**Costo estimado por llamada: USD 0.002598** (COP 10.4). Fin."
    )
