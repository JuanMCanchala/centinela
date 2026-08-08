"""Comprueba que los números de los documentos sigan siendo ciertos.

Por qué existe. El README decía "55 tests" cuando había 171, y en otro sitio del mismo
archivo decía "161". Ninguna de las dos era mentira cuando se escribió: eran ciertas y
se quedaron rancias. La rúbrica advierte que lo reportado se contrasta con los logs de
la sesión, así que una cifra rancia no es un descuido de estilo, es una discrepancia
entre el informe y el sistema.

El problema no se arregla corrigiendo los números: se arregla haciendo que no se puedan
quedar rancios sin que algo falle. Este script lee las cifras que los documentos
afirman, las compara con la medición real, y devuelve 1 si alguna no cuadra.

Qué comprueba, y de dónde saca la verdad:

  tests            `pytest --collect-only` cuenta los tests de verdad
  casos oficiales  docs/metrics/triage_160_casos.json (lo escribe `make eval`)
  adversarial      docs/metrics/redteam.json (lo escribe `make redteam`)
  cobertura RAG    docs/metrics/rag_cobertura.json (lo escribe `make rag`)
  tendencia        docs/metrics/tendencia.json (lo escribe `make tendencia`)
  locuciones       el guion clínico, contando las que existen

Lo que NO comprueba: las cifras de latencia y consumo. Esas ya vienen de
`docs/metricas.md`, que se genera del mismo `runtime.json`, y el README lo dice.

    python scripts/verificar_cifras.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))

METRICAS = RAIZ / "docs" / "metrics"


@dataclass
class Comprobacion:
    nombre: str
    esperado: object
    afirmado: object
    donde: str

    @property
    def cuadra(self) -> bool:
        return str(self.esperado) == str(self.afirmado)


def leer(ruta: Path) -> dict:
    return json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else {}


def contar_tests() -> int | None:
    """Cuántos tests hay de verdad. `--collect-only` no los ejecuta."""

    py = RAIZ / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        py = RAIZ / ".venv" / "bin" / "python"
    try:
        r = subprocess.run(
            [str(py), "-m", "pytest", "-q", "--collect-only"],
            cwd=RAIZ, capture_output=True, text=True, timeout=180,
        )
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"(\d+) tests? collected", r.stdout)
    return int(m.group(1)) if m else None


def contar_locuciones() -> int:
    """Las que se pre-renderizan de verdad.

    Tiene que contar lo mismo que `main.py`: el guion clinico Y las muletillas de
    naturalidad. Contando solo el guion daba 40 y marcaba como rancio un 53 que era
    correcto -- salvo por uno, que es justo lo que este script existe para ver.
    """

    from centinela.dialog import script as S

    return len(S.todas_las_locuciones()) + len(S.naturalidad())


# Texto entre comillas angulares. Un numero citado no es un numero afirmado: el propio
# informe cuenta que el README llego a decir «55 tests», y sin esta regla ese relato se
# marcaba como cifra rancia. Contar una cita como afirmacion haria imposible documentar
# un error pasado.
RE_CITA = re.compile(r"«[^»]*»")


def cifras_afirmadas(texto: str, patron: str) -> list[str]:
    """Todas las cifras que el documento AFIRMA para un concepto dado."""

    return re.findall(patron, RE_CITA.sub(" ", texto))


def main() -> int:
    readme = (RAIZ / "README.md").read_text(encoding="utf-8")
    informe = (RAIZ / "docs" / "informe-final.md").read_text(encoding="utf-8")

    triage = leer(METRICAS / "triage_160_casos.json")
    redteam = leer(METRICAS / "redteam.json")
    rag = leer(METRICAS / "rag_cobertura.json")

    comprobaciones: list[Comprobacion] = []

    # --- tests -------------------------------------------------------------
    n_tests = contar_tests()
    if n_tests is not None:
        for doc, texto in (("README.md", readme), ("informe-final.md", informe)):
            for afirmado in cifras_afirmadas(texto, r"(\d+)\s+tests"):
                comprobaciones.append(
                    Comprobacion("tests", n_tests, afirmado, doc)
                )
            for afirmado in cifras_afirmadas(texto, r"make test.*?(\d+)\s*/\s*\1"):
                comprobaciones.append(Comprobacion("tests", n_tests, afirmado, doc))

    # --- casos oficiales ---------------------------------------------------
    if triage:
        # `exactitud_global` viene como proporcion; los documentos hablan de "152/160",
        # asi que se reconstruye el numerador con el n de casos de la propia medicion.
        n_casos = triage.get("n_casos")
        exactitud = triage.get("exactitud_global")
        aciertos = round(exactitud * n_casos) if (n_casos and exactitud) else None
        if aciertos:
            for doc, texto in (("README.md", readme), ("informe-final.md", informe)):
                for afirmado in cifras_afirmadas(texto, r"(\d+)/160"):
                    comprobaciones.append(
                        Comprobacion("aciertos sobre 160", aciertos, afirmado, doc)
                    )
        fn = triage.get("falsos_negativos_clinicos")
        if fn is not None and fn != 0:
            comprobaciones.append(
                Comprobacion("falsos negativos clínicos", 0, fn, "medición")
            )

    # --- adversarial -------------------------------------------------------
    if redteam:
        total = redteam.get("n_casos")
        pasan = redteam.get("pasan")
        if total:
            for doc, texto in (("README.md", readme), ("informe-final.md", informe)):
                for afirmado in cifras_afirmadas(texto, r"(\d+)\s+casos adversariales"):
                    comprobaciones.append(
                        Comprobacion("casos adversariales", total, afirmado, doc)
                    )
        if pasan is not None and total and pasan != total:
            comprobaciones.append(
                Comprobacion("adversarial que pasan", total, pasan, "medición")
            )

    # --- cobertura del RAG -------------------------------------------------
    if rag:
        for clave, nombre in (
            ("citas_cruzadas", "citas de otro procedimiento"),
            ("cifras_sin_respaldo", "cifras sin respaldo"),
            ("cifras_mal_contextualizadas", "cifras con otra unidad"),
        ):
            valor = rag.get(clave)
            if valor:
                comprobaciones.append(Comprobacion(nombre, 0, valor, "medición"))

    # --- locuciones pre-renderizadas ---------------------------------------
    n_loc = contar_locuciones()
    for doc, texto in (("README.md", readme), ("informe-final.md", informe)):
        for afirmado in cifras_afirmadas(texto, r"\*\*(\d+)\s+locuciones\*\*"):
            comprobaciones.append(Comprobacion("locuciones", n_loc, afirmado, doc))

    # ----------------------------------------------------------------------
    print("=" * 78)
    print("CIFRAS DE LOS DOCUMENTOS CONTRA LA MEDICION")
    print("=" * 78)
    print()

    if not comprobaciones:
        print("  No se encontro ninguna cifra comprobable.")
        print("  Corra `make eval`, `make redteam` y `make rag` para generar las")
        print("  mediciones, y vuelva a intentarlo.")
        return 0

    malas = [c for c in comprobaciones if not c.cuadra]

    for c in comprobaciones:
        marca = "OK  " if c.cuadra else "MAL "
        print(f"  {marca} {c.nombre:32s} dice {str(c.afirmado):>8s} "
              f"y es {str(c.esperado):>8s}   ({c.donde})")

    print()
    if malas:
        print(f"  {len(malas)} cifra(s) de los documentos ya no son ciertas.")
        print("  Corregirlas, o regenerar la medicion si lo que cambio es el sistema.")
    else:
        print(f"  {len(comprobaciones)} cifras comprobadas, todas cuadran.")

    return 1 if malas else 0


if __name__ == "__main__":
    raise SystemExit(main())
