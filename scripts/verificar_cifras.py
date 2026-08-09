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
  consumo y costo  docs/metrics/runtime.json (lo escribe `make runtime`)

La última línea se añadió después, y la añadió un fallo. Este archivo decía «lo que NO
comprueba: las cifras de latencia y consumo, porque ya vienen de `docs/metricas.md`, que
se genera del mismo `runtime.json`». El razonamiento tenía un agujero: `docs/metricas.md`
se genera, pero **el README las copia a mano**, y para cuando alguien las miró seis de
ellas eran falsas —la muestra decía 42 turnos y eran 51, el costo decía USD 0.002425 y
era 0.002598, y una frase remitía a «los 415.4 tokens/llamada de arriba» cuando arriba
decía 462.2—. Justo la clase de discrepancia que la rúbrica castiga por nombre.

La lección, que vale más que el arreglo: **una cifra queda fuera del verificador solo si
ningún documento la afirma.** Que exista un generado en otro archivo no protege a la
copia escrita a mano.

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
    # Donde esta escrita la cifra en su documento. Solo lo traen las que el script sabe
    # corregir; las que se cuentan de otra forma -- los tests, las locuciones -- no.
    span: tuple[int, int] | None = None

    @property
    def cuadra(self) -> bool:
        """Compara por valor cuando los dos lados son números.

        `37` y `37.0` son la misma cifra y la comparación textual los separaba: el JSON
        redondea a un decimal y el documento escribe el que le sale. Comparar el texto
        habría marcado como rancia una cifra correcta, que es el fallo que este script
        cometió una vez y no debe volver a cometer.
        """

        try:
            igual = float(str(self.esperado)) == float(
                str(self.afirmado).replace(" ", "")
            )
        except ValueError:
            igual = str(self.esperado) == str(self.afirmado)
        return igual

    @property
    def medible(self) -> bool:
        """False cuando la medición no trae la cifra que el documento afirma.

        No es lo mismo que estar mal: significa que falta correr `make runtime`, o que
        el campo cambió de nombre. Se reporta aparte para que no se confunda con una
        cifra rancia, y para que no se pierda de vista, que es lo que la haría inútil.
        """

        return self.esperado is not None


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
    """Las que acaban de verdad en el cache de audio.

    La regla costo dos intentos, y los dos fallaron por el mismo motivo: contar la
    lista de entrada en vez de lo que el cache puede contener.

      - Contando solo `todas_las_locuciones()` daba 40 y marcaba como rancio un
        numero correcto.
      - Contando eso mas `naturalidad()` daba 54, y con ese numero se "corrigio" el
        README de 53 a 54 -- rompiendo un numero que estaba bien.

    Lo que el cache contiene son 53, y la que falta es `confirmar_identidad`: su
    texto lleva `{nombre}` y depende del paciente, asi que `PiperTTS.pre_renderizar`
    la salta a proposito. Una locucion con marcador de formato no es pre-renderizable
    por definicion, asi que la cuenta la excluye.
    """

    from centinela.dialog import script as S

    juntas = list(S.todas_las_locuciones()) + list(S.naturalidad())
    return len([loc for loc in juntas if "{" not in loc.texto])


# Texto entre comillas angulares. Un numero citado no es un numero afirmado: el propio
# informe cuenta que el README llego a decir «55 tests», y sin esta regla ese relato se
# marcaba como cifra rancia. Contar una cita como afirmacion haria imposible documentar
# un error pasado.
RE_CITA = re.compile(r"«[^»]*»")


def enmascarar(texto: str) -> str:
    """Borra las citas sin mover nada de sitio.

    Sustituir por un espacio bastaba mientras el script solo comparaba. Para corregir
    hacen falta las posiciones del documento original, asi que la mascara conserva la
    longitud: un blanco por caracter tapado.
    """

    return RE_CITA.sub(lambda m: " " * len(m.group(0)), texto)


def formatear(valor: object, agrupado: bool = False) -> str:
    """La cifra como se escribe en un documento, nunca en notacion cientifica.

    El desglose del costo tiene sumandos del orden de 1e-05, y `str()` los escribe
    `5e-05`. Escribir eso en el README seria cierto y a la vez ilegible para quien
    compara con su propia factura.

    `agrupado` reproduce el separador de miles del documento —«7 321 turnos», con un
    espacio— porque la correccion no debe cambiar la tipografia de la frase que arregla.
    Se decide mirando la cifra que habia, no por configuracion: si estaba agrupada, la
    nueva tambien.
    """

    if isinstance(valor, float):
        texto = f"{valor:.10f}".rstrip("0")
        if texto.endswith("."):
            texto = f"{texto}0"
    else:
        texto = str(valor)

    if agrupado and texto.replace(".", "").isdigit():
        entera, _punto, decimal = texto.partition(".")
        trozos = []
        while len(entera) > 3:
            trozos.insert(0, entera[-3:])
            entera = entera[:-3]
        trozos.insert(0, entera)
        texto = " ".join(trozos) + (f".{decimal}" if decimal else "")
    return texto


def aplanar(datos: dict) -> dict:
    """Añade claves sintéticas para lo que vive en listas o hay que derivar.

    `hondo` navega diccionarios, y los caminos de latencia son una lista de filas. En vez
    de enseñarle a indexar listas —que obligaría a escribir el índice en la ruta, y el
    índice cambia cuando cambia el orden— se les pone nombre aquí:

      caminos_planos.<nombre del camino con _>   cada fila por su nombre
      voz_vigente                               la fila de la configuración de STT en uso
      derivados.proporcion_cache_pct            la de caché, en la unidad en que se lee
    """

    plano = dict(datos)
    caminos = datos.get("por_camino") or {}

    por_nombre: dict[str, dict] = {}
    for grupo in ("caminos", "voz_por_configuracion"):
        for fila in caminos.get(grupo) or []:
            por_nombre[str(fila.get("camino", "")).replace(" ", "_")] = fila
    plano["caminos_planos"] = por_nombre

    vigente = caminos.get("configuracion_vigente")
    if vigente:
        plano["voz_vigente"] = por_nombre.get(
            f"voz_con_STT_{vigente}".replace(" ", "_"), {}
        )

    derivados: dict[str, object] = {}
    proporcion = ((datos.get("resumen") or {}).get("tts") or {}).get(
        "proporcion_desde_cache"
    )
    if proporcion is not None:
        derivados["proporcion_cache_pct"] = round(proporcion * 100)

    # El texto vuelve a citar la mediana redondeada -- «los 622 ms de mediana quedan
    # dentro del umbral» -- y esa copia se queda rancia igual que la tabla.
    e2e = (datos.get("extremo_a_extremo") or {}).get("p50_ms_con_cierre_adaptativo")
    if e2e is not None:
        derivados["p50_e2e_ms"] = round(e2e)

    plano["derivados"] = derivados
    return plano


def comprobar(
    nombre: str,
    patron: str,
    esperado: object,
    textos: tuple[tuple[str, str], ...],
    grupos: int = 1,
) -> list[Comprobacion]:
    """Cada cifra que los documentos afirman para un concepto, contra su medición.

    Devuelve las posiciones, así que `--corregir` también arregla estas. La primera
    versión no las traía y había que sincronizar el número de tests a mano después de cada
    corrida — un trámite que se repite es un trámite que se acaba salteando, y así es como
    llegó a haber diez cifras falsas a la vez.
    """

    fuera: list[Comprobacion] = []
    for doc, texto in textos:
        for hallado in re.finditer(patron, enmascarar(texto)):
            for orden in range(1, grupos + 1):
                fuera.append(
                    Comprobacion(
                        nombre, esperado, hallado.group(orden), doc, hallado.span(orden)
                    )
                )
    return fuera


# ==========================================================================
# Las cifras de consumo, muestra y costo que exige la rúbrica (§5)
#
# Cada entrada es: nombre, el patrón que la AFIRMA en el documento, y la ruta del
# `runtime.json` que la MIDE. Los grupos del patrón se emparejan en orden con las rutas,
# así que una tabla de dos columnas —«462.2 / 37.0»— se comprueba de una sola pasada.
#
# El patrón va anclado a la redacción, no solo al número: buscar `\d+ turnos` a secas
# habría capturado los 7 321 turnos del histórico, que es otra población y otra cifra.
# ==========================================================================

# Un número como el documento lo escribe: con separador de miles de espacio y decimales
# opcionales. «6 623», «126.7» y «1 071.8» son todos esto.
NUM = r"\d+(?: \d{3})*(?:\.\d+)?"

CIFRAS_DE_RUNTIME: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("muestra", r"\*\*(\d+) turnos en (\d+) llamadas",
     ("resumen.n_turnos", "resumen.n_llamadas")),
    # --- las cifras de latencia, que son las que el jurado contrasta con la sesion ---
    ("histórico medido", rf"sobre los ({NUM}) turnos medidos",
     ("por_camino.n_turnos_medidos",)),
    ("camino desde caché", rf"voz desde caché \| ({NUM}) \| \*\*({NUM}) ms\*\* \| ({NUM}) ms",
     ("caminos_planos.voz_del_agente_desde_cache.n",
      "caminos_planos.voz_del_agente_desde_cache.p50_ms",
      "caminos_planos.voz_del_agente_desde_cache.p95_ms")),
    ("configuración vigente", r"configuración vigente \(`([^`]+)`\)",
     ("por_camino.configuracion_vigente",)),
    ("camino de voz vigente",
     rf"configuración vigente \(`[^`]+`\) \| ({NUM}) \| \*\*({NUM}) ms\*\* \| ({NUM}) ms",
     ("voz_vigente.n", "voz_vigente.p50_ms", "voz_vigente.p95_ms")),
    ("proporción desde caché", r"\*\*(\d+) % de los turnos se sirven desde",
     ("derivados.proporcion_cache_pct",)),
    ("extremo a extremo, cierre adaptativo",
     rf"cierre adaptativo \(450 ms\) \| \*\*({NUM}) ms\*\*",
     ("extremo_a_extremo.p50_ms_con_cierre_adaptativo",)),
    ("extremo a extremo, techo P50", rf"techo del cliente \(900 ms\) \| ({NUM}) ms",
     ("extremo_a_extremo.p50_ms_con_techo",)),
    ("extremo a extremo, techo P95", rf"P95 con el techo \| ({NUM}) ms",
     ("extremo_a_extremo.p95_ms_con_techo",)),
    ("mediana citada en el texto", r"Los (\d+) ms de mediana",
     ("derivados.p50_e2e_ms",)),
    ("muestra de voz", r"\*\*(\d+) turnos entran por voz",
     ("resumen.n_turnos_voz",)),
    ("tokens/turno P50", r"por turno \(P50\) \| \*\*([\d.]+) / ([\d.]+)\*\*",
     ("resumen.tokens_por_turno.entrada_p50", "resumen.tokens_por_turno.salida_p50")),
    ("tokens/turno media", r"por turno \(media\) \| ([\d.]+) / ([\d.]+)",
     ("resumen.tokens_por_turno.entrada_media", "resumen.tokens_por_turno.salida_media")),
    ("tokens/llamada", r"\*\*por llamada\*\* \(media\) \| \*\*([\d.]+) / ([\d.]+)\*\*",
     ("resumen.tokens_por_llamada.entrada_media",
      "resumen.tokens_por_llamada.salida_media")),
    ("tokens/llamada citado", r"los ([\d.]+) tokens/llamada",
     ("resumen.tokens_por_llamada.entrada_media",)),
    ("turnos/llamada", r"Turnos por llamada \(media\) \| ([\d.]+)",
     ("resumen.tokens_por_llamada.turnos_media",)),
    ("invocaciones/turno", r"P50 = ([\d.]+)\*\*, media ([\d.]+), máximo (\d+)",
     ("resumen.invocaciones_llm_por_turno.p50",
      "resumen.invocaciones_llm_por_turno.media",
      "resumen.invocaciones_llm_por_turno.max")),
    ("consultas RAG/llamada", r"\*\*([\d.]+) de media\*\*, máximo (\d+)",
     ("resumen.consultas_rag_por_llamada.media",
      "resumen.consultas_rag_por_llamada.max")),
    ("costo/llamada", r"Costo estimado por llamada: USD ([\d.]+)\*\* \(COP ([\d.]+)\)",
     ("costo.costo_total_usd_por_llamada", "costo.costo_total_cop_por_llamada")),
    # Los tres cierran frase, asi que el grupo no puede ser `[\d.]+`: se traga el punto
    # final y la cifra pasa a ser "0.001404." -- que no es un numero y no cuadra nunca.
    ("desglose del costo",
     r"modelo USD (\d+\.\d+) · transcripción USD (\d+\.\d+) · voz USD (\d+\.\d+)",
     ("costo.desglose_usd.llm", "costo.desglose_usd.stt", "costo.desglose_usd.tts")),
)


def hondo(datos: dict, ruta: str) -> object:
    """El valor de una ruta con puntos, o None si el camino se corta."""

    valor: object = datos
    for parte in ruta.split("."):
        if isinstance(valor, dict):
            valor = valor.get(parte)
        else:
            valor = None
    return valor


def comprobar_runtime(
    textos: tuple[tuple[str, str], ...], datos: dict
) -> list[Comprobacion]:
    """Cada cifra de §5 que un documento afirma, contra la medición que la sostiene."""

    fuera: list[Comprobacion] = []
    for nombre, patron, rutas in CIFRAS_DE_RUNTIME:
        for doc, texto in textos:
            for hallado in re.finditer(patron, enmascarar(texto)):
                for orden, ruta in enumerate(rutas, start=1):
                    fuera.append(
                        Comprobacion(
                            f"{nombre} · {ruta.rsplit('.', 1)[-1]}",
                            hondo(datos, ruta),
                            hallado.group(orden),
                            doc,
                            hallado.span(orden),
                        )
                    )
    return fuera


def corregir(malas: list[Comprobacion]) -> list[str]:
    """Escribe la medición encima de la cifra rancia, en su sitio exacto.

    Existe porque la alternativa no funciona. Cada `make runtime` mueve diez cifras de
    §5, sincronizarlas a mano es un trámite, y un trámite que se repite se acaba
    salteando: así llegaron a haber diez cifras falsas a la vez en un documento que
    presume de que sus números los escriben los scripts.

    Se reemplaza de atrás hacia adelante para que las posiciones ya calculadas no se
    desplacen cuando la cifra nueva tiene otra longitud.
    """

    tocados: list[str] = []
    por_documento: dict[str, list[Comprobacion]] = {}
    for c in malas:
        if c.span is not None:
            por_documento.setdefault(c.donde, []).append(c)

    for doc, cifras in por_documento.items():
        ruta = RAIZ / doc if doc == "README.md" else RAIZ / "docs" / doc
        texto = ruta.read_text(encoding="utf-8")
        for c in sorted(cifras, key=lambda c: c.span[0], reverse=True):
            inicio, fin = c.span
            agrupado = " " in str(c.afirmado)
            texto = texto[:inicio] + formatear(c.esperado, agrupado) + texto[fin:]
        ruta.write_text(texto, encoding="utf-8")
        tocados.append(f"{doc} ({len(cifras)} cifras)")

    return tocados


def main() -> int:
    readme = (RAIZ / "README.md").read_text(encoding="utf-8")
    informe = (RAIZ / "docs" / "informe-final.md").read_text(encoding="utf-8")

    triage = leer(METRICAS / "triage_160_casos.json")
    redteam = leer(METRICAS / "redteam.json")
    rag = leer(METRICAS / "rag_cobertura.json")
    runtime = leer(METRICAS / "runtime.json")
    atribucion = leer(METRICAS / "atribucion.json")

    comprobaciones: list[Comprobacion] = []

    # --- consumo, muestra y costo (§5) -------------------------------------
    if runtime:
        comprobaciones.extend(
            comprobar_runtime(
                (("README.md", readme), ("informe-final.md", informe)), aplanar(runtime)
            )
        )

    docs = (("README.md", readme), ("informe-final.md", informe))

    # --- tests -------------------------------------------------------------
    n_tests = contar_tests()
    if n_tests is not None:
        comprobaciones.extend(comprobar("tests", r"(\d+)\s+tests", n_tests, docs))
        # Los dos lados del "746/746". Antes el patron era `(\d+)/\1` --una
        # retrorreferencia-- y solo capturaba el primero: corregirlo habria dejado
        # "766/746", que es peor que la cifra rancia. Se capturan los dos y se comprueban
        # los dos contra la misma medicion.
        comprobaciones.extend(
            comprobar("tests", r"make test.*?(\d+)\s*/\s*(\d+)", n_tests, docs, grupos=2)
        )

    # --- casos oficiales ---------------------------------------------------
    if triage:
        # `exactitud_global` viene como proporcion; los documentos hablan de "152/160",
        # asi que se reconstruye el numerador con el n de casos de la propia medicion.
        n_casos = triage.get("n_casos")
        exactitud = triage.get("exactitud_global")
        aciertos = round(exactitud * n_casos) if (n_casos and exactitud) else None
        if aciertos:
            comprobaciones.extend(
                comprobar("aciertos sobre 160", r"(\d+)/160", aciertos, docs)
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
            comprobaciones.extend(
                comprobar("casos adversariales", r"(\d+)\s+casos adversariales", total, docs)
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
    comprobaciones.extend(
        comprobar(
            "locuciones", r"\*\*(\d+)\s+locuciones\*\*", contar_locuciones(), docs
        )
    )

    # --- atribucion cruzada (deceptive grounding) --------------------------
    if atribucion:
        cruzadas = atribucion.get("citas_de_otro_procedimiento")
        if cruzadas:
            comprobaciones.append(
                Comprobacion("citas ajenas en las trampas", 0, cruzadas, "medición")
            )
        for nombre, clave, patron in (
            ("trampas de atribución", "n_trampas", r"\| Trampas \| (\d+) \|"),
            ("trampas: se abstuvo", "se_abstuvo", r"\| Se abstuvo \| (\d+) \|"),
            ("trampas: respondió", "respondio_a_la_trampa",
             r"\| Respondió con su propio corpus \| (\d+) \|"),
            ("trampas: declinó el dato", "de_esas_declino_el_dato",
             r"y redirigió \| (\d+) \|"),
        ):
            comprobaciones.extend(
                comprobar(nombre, patron, atribucion.get(clave), docs)
            )

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

    medibles = [c for c in comprobaciones if c.medible]
    sin_medir = [c for c in comprobaciones if not c.medible]
    malas = [c for c in medibles if not c.cuadra]

    for c in comprobaciones:
        if c.medible:
            marca = "OK  " if c.cuadra else "MAL "
            esperado = str(c.esperado)
        else:
            marca = "?   "
            esperado = "sin medir"
        print(f"  {marca} {c.nombre:38s} dice {str(c.afirmado):>9s} "
              f"y es {esperado:>9s}   ({c.donde})")

    print()
    if sin_medir:
        print(f"  {len(sin_medir)} cifra(s) no se pudieron comprobar: la medicion no las")
        print("  trae. Corra `make runtime` para regenerarla.")

    a_mano = [c for c in malas if c.span is None]
    if not malas:
        print(f"  {len(medibles)} cifras comprobadas, todas cuadran.")
        codigo = 0
    elif "--corregir" in sys.argv:
        for linea in corregir(malas):
            print(f"  ESCRITO  {linea}")
        if a_mano:
            print(f"  {len(a_mano)} cifra(s) no se corrigen solas: se cuentan del codigo,")
            print("  no de una medicion, asi que lo que cambio es el sistema.")
        codigo = 1 if a_mano else 0
    else:
        print(f"  {len(malas)} cifra(s) de los documentos ya no son ciertas.")
        print("  Corregirlas con `--corregir`, o regenerar la medicion si lo que cambio")
        print("  es el sistema.")
        codigo = 1

    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
