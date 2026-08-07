"""Comprueba que cada elemento de los diagramas existe en el codigo.

La rubrica dice: *"Si tu diagrama corresponde a lo que realmente implementaste. El
jurado toma elementos del diagrama al azar y los busca en el codigo."*

Este script hace ese trabajo antes que el jurado. Recorre los diagramas mermaid
de `docs/arquitectura.md`, extrae todas las referencias con forma de ruta de
modulo (`clinical/triage_engine.py`) o de simbolo (`TriageEngine.evaluar`), y
verifica que cada una exista de verdad. Falla con codigo distinto de cero si
alguna no existe, asi que un diagrama que se desactualiza rompe la verificacion.

    python scripts/verificar_diagrama.py
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
DIR_API = RAIZ / "api" / "centinela"
DOCS = (RAIZ / "docs" / "arquitectura.md",)

# Rutas de modulo dentro de los diagramas: "clinical/triage_engine.py"
RE_MODULO = re.compile(r"\b([a-z_]+/[a-z_]+\.py)\b")
# Simbolos: "TriageEngine.evaluar", "normalizar_turno", "_interrumpir_por_bandera_roja"
RE_SIMBOLO = re.compile(r"\b([A-Z][A-Za-z0-9]+\.[a-z_][a-z_0-9]*|_?[a-z][a-z_0-9]{4,})\b")

# Palabras que aparecen en los diagramas y no son simbolos del codigo.
IGNORAR = frozenset("""
    direction flowchart subgraph sequenceDiagram participant note over
    llamada consola trazabilidad navegador persistencia decision percepcion
    contrato turno datos inmunidad ragsub texto libre guion primer byte audio
    fin habla cierre reglas metricas ticket resumen borrado ingesta recibo
    modelo lenguaje ollama chromadb sqlite documentos chunks auditoria recibos
    llamadas tickets incidentes minimo maximo pagina paginas frase frases
    campos nada mas capa capas dominio dominios criticidad bandera banderas
    alarma vigilancia paciente agente jurado codigo indice lexico
    numerica colombiano esquema forzado denso lexica compuerta
    fundamentacion generacion residuales verificado turnos intentos
    quedan agotados siguiente cierra pregunta reintento profundizar
    instruccion seguridad pasa ahora contacto puede descartar
    preguntar preguntas responder respondio sirve hacer falta
    reglas resolvieron mayoria relleno cacheado detectado
    manipulacion peor resultado posible campo extraido
    micro microfono subir listar borrar
""".split())


def simbolos_del_codigo() -> tuple[set[str], set[str]]:
    """Modulos y simbolos publicos definidos en api/centinela."""

    modulos: set[str] = set()
    simbolos: set[str] = set()

    for py in DIR_API.rglob("*.py"):
        rel = py.relative_to(DIR_API).as_posix()
        modulos.add(rel)
        modulos.add(py.name)

        arbol = ast.parse(py.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                simbolos.add(nodo.name)
            elif isinstance(nodo, ast.ClassDef):
                simbolos.add(nodo.name)
                for hijo in nodo.body:
                    if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        simbolos.add(hijo.name)
                        simbolos.add(f"{nodo.name}.{hijo.name}")
            elif isinstance(nodo, ast.Assign):
                for t in nodo.targets:
                    if isinstance(t, ast.Name):
                        simbolos.add(t.id)
            elif isinstance(nodo, ast.AnnAssign) and isinstance(nodo.target, ast.Name):
                # Campos de dataclass y de modelos pydantic.
                simbolos.add(nodo.target.id)
            elif isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
                # Claves de diccionario y literales que el codigo expone como
                # parte de su contrato: los diagramas los referencian igual que
                # una funcion ("olvido_verificado", "vectores_residuales"), y son
                # tan verificables como ella -- estan en el codigo, literalmente.
                texto = nodo.value
                if 4 <= len(texto) <= 40 and texto.replace("_", "").isalnum():
                    simbolos.add(texto)

    # Modulos de web/ y scripts/ tambien se referencian en los diagramas.
    for otro in (RAIZ / "web", RAIZ / "scripts", RAIZ / "eval"):
        for f in otro.rglob("*"):
            if f.is_file():
                modulos.add(f.name)
                modulos.add(f.relative_to(RAIZ).as_posix())

    return modulos, simbolos


def referencias_del_diagrama(texto: str) -> tuple[set[str], set[str]]:
    """Extrae solo lo que esta dentro de bloques mermaid."""

    bloques = re.findall(r"```mermaid\n(.*?)```", texto, re.DOTALL)
    contenido = "\n".join(bloques)

    mods = set(RE_MODULO.findall(contenido))
    simbolos = {
        s for s in RE_SIMBOLO.findall(contenido)
        if s.lower() not in IGNORAR and not s.endswith(".py")
    }
    # Se descartan las palabras sueltas en minuscula que no parecen simbolos de
    # codigo: solo interesan los que llevan punto o guion bajo.
    simbolos = {s for s in simbolos if "." in s or "_" in s or s[0].isupper()}
    return mods, simbolos


def main() -> int:
    modulos_reales, simbolos_reales = simbolos_del_codigo()
    print(f"codigo: {len(modulos_reales)} rutas de modulo, "
          f"{len(simbolos_reales)} simbolos definidos")

    faltan_modulos: list[tuple[str, str]] = []
    faltan_simbolos: list[tuple[str, str]] = []
    total_mods = 0
    total_simbolos = 0

    for doc in DOCS:
        if not doc.exists():
            print(f"AVISO: no existe {doc.relative_to(RAIZ)}")
        else:
            texto = doc.read_text(encoding="utf-8")
            mods, simbolos = referencias_del_diagrama(texto)
            total_mods += len(mods)
            total_simbolos += len(simbolos)
            print(f"\n{doc.relative_to(RAIZ)}: {len(mods)} modulos, "
                  f"{len(simbolos)} simbolos referenciados")

            for m in sorted(mods):
                existe = m in modulos_reales or Path(m).name in modulos_reales
                if not existe:
                    faltan_modulos.append((doc.name, m))
                    print(f"  FALTA modulo  {m}")

            for s in sorted(simbolos):
                base = s.split(".")[-1]
                # Un nombre de modulo mencionado sin la extension ("triage_engine"
                # en el cuerpo de un texto) es una referencia valida: la regex de
                # simbolos no puede distinguirlo, la comprobacion si.
                como_modulo = f"{base}.py" in modulos_reales
                existe = s in simbolos_reales or base in simbolos_reales or como_modulo
                if not existe:
                    faltan_simbolos.append((doc.name, s))
                    print(f"  FALTA simbolo {s}")

    print()
    print("=" * 74)
    ok_mods = total_mods - len(faltan_modulos)
    ok_simbolos = total_simbolos - len(faltan_simbolos)
    print(f"  modulos referenciados que existen : {ok_mods}/{total_mods}")
    print(f"  simbolos referenciados que existen: {ok_simbolos}/{total_simbolos}")

    if faltan_modulos or faltan_simbolos:
        print()
        print("  El diagrama referencia cosas que no estan en el codigo.")
        print("  Hay que corregir el diagrama o implementar lo que promete.")
        codigo = 1
    else:
        print()
        print("  Todo elemento del diagrama existe en el codigo.")
        codigo = 0
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
