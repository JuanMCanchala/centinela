"""Descarga las tipografias del panel a `web/fuentes/`, para servirlas nosotros.

Por que vendorizar en vez de enlazar a Google Fonts:

  1. El sistema entero corre local a proposito -- STT, LLM, TTS, indice vectorial.
     Que la interfaz dependiera de una CDN seria la unica pieza que necesita
     internet, y ademas la mas visible si falla.
  2. La demo se graba en video. Una tipografia que no carga cambia el layout
     entero a mitad de grabacion.
  3. Pedirle la fuente a Google en cada carga filtra la IP del hospital a un
     tercero. En un producto clinico eso hay que justificarlo, y no hace falta.

Las dos familias son latinoamericanas, y no por casualidad: es un producto clinico
en espanol.

  - Montserrat (Julieta Ulanovsky, Argentina) para la voz de interfaz. Es una
    geometrica sacada de la carteleria del barrio de Montserrat en Buenos Aires.
  - Chivo Mono (Omnibus-Type, Argentina) para datos y transcripciones. Casi todo
    el texto de esta consola es de este tipo: numeros, sha, horas, y las
    transcripciones, que van en mono a proposito porque una transcripcion es
    prueba y se cita literal.

Las dos bajo SIL Open Font License 1.1.

Reparto deliberado: Montserrat es geometrica, ancha y de x alta -- excelente en
titulos y nombres, mala para una tabla de metadatos a 11 px. La mono se queda con
todo lo denso.

Uso:  python scripts/fetch_fuentes.py
"""

from __future__ import annotations

import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
DESTINO = RAIZ / "web" / "fuentes"

# Un navegador de verdad en el User-Agent: la API css2 decide el formato que
# sirve segun quien pregunta. Con un UA generico devuelve TTF sin comprimir
# (~4x mas pesado); con uno de Chrome devuelve woff2 ya subseteado.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

FAMILIAS = (
    ("Montserrat", "https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700;800"),
    ("Chivo Mono", "https://fonts.googleapis.com/css2?family=Chivo+Mono:wght@400;500;700"),
)

# Google sirve cada familia partida en subconjuntos Unicode. Montserrat trae
# cirilico y vietnamita, que suman 63 KB que este proyecto no va a usar jamas: el
# corpus es clinico en espanol y los nombres de paciente tambien. El navegador no
# los bajaria (el unicode-range se lo impide), pero si quedarian versionados en el
# repo sin motivo. latin-ext cubre el espanol completo con margen de sobra.
SUBCONJUNTOS = frozenset({"latin", "latin-ext"})

CABECERA = """/* Tipografias del panel, servidas localmente.
 *
 * Montserrat -- Julieta Ulanovsky. Chivo Mono -- Omnibus-Type.
 * Las dos bajo SIL Open Font License 1.1.
 * Generado por scripts/fetch_fuentes.py. No editar a mano.
 *
 * Son fuentes variables: un solo archivo por subconjunto Unicode cubre todo el
 * eje de peso 400-900, asi que las cuatro declaraciones de peso apuntan al mismo
 * .woff2. Se conserva el unicode-range de Google para que el navegador no baje
 * el subconjunto latin-ext salvo que la pagina lo necesite.
 */
"""


def _bajar(url: str, intentos: int = 3) -> bytes:
    """GET con reintentos. La resolucion DNS de esta maquina falla a veces."""

    ultimo: Exception | None = None
    datos = b""
    for intento in range(intentos):
        if not datos:
            try:
                pedido = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(pedido, timeout=30) as respuesta:
                    datos = respuesta.read()
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                ultimo = exc
                print(f"    reintento {intento + 1}/{intentos}: {exc}")
    if not datos:
        raise RuntimeError(f"no se pudo bajar {url}") from ultimo
    return datos


def main() -> int:
    DESTINO.mkdir(parents=True, exist_ok=True)
    piezas: list[str] = [CABECERA]
    total = 0

    for familia, url_css in FAMILIAS:
        print(f"{familia}")
        css = _bajar(url_css).decode("utf-8")

        # La CSS de Google viene como  /* subset */ @font-face { ... }  repetido.
        # Cada bloque trae su unicode-range, que es la parte que vale la pena
        # conservar: sin el, el navegador baja todos los subconjuntos siempre.
        bloques = re.finditer(r"/\* (\S+) \*/\s*@font-face \{(.*?)\}", css, re.S)
        ya_bajados: dict[str, str] = {}

        for bloque in bloques:
            subconjunto, cuerpo = bloque.group(1), bloque.group(2)
            if subconjunto in SUBCONJUNTOS:
                url_fuente = re.search(r"url\((https[^)]+)\)", cuerpo).group(1)
                rango = re.search(r"unicode-range: ([^;]+);", cuerpo)
                peso = re.search(r"font-weight: (\d+)", cuerpo)

                if url_fuente not in ya_bajados:
                    nombre = f"{familia.lower().replace(' ', '-')}-{subconjunto}.woff2"
                    archivo = DESTINO / nombre
                    if not archivo.exists():
                        archivo.write_bytes(_bajar(url_fuente))
                        total += archivo.stat().st_size
                    ya_bajados[url_fuente] = nombre
                    print(f"    {nombre:34s} {archivo.stat().st_size:>7,d} B  {subconjunto}")

                piezas.append(
                    "@font-face {\n"
                    f"  font-family: '{familia}';\n"
                    "  font-style: normal;\n"
                    f"  font-weight: {peso.group(1) if peso else '400'};\n"
                    "  font-display: swap;\n"
                    f"  src: url(/estatico/fuentes/{ya_bajados[url_fuente]}) format('woff2');\n"
                    + (f"  unicode-range: {rango.group(1)};\n" if rango else "")
                    + "}\n"
                )

    (DESTINO / "fuentes.css").write_text("\n".join(piezas), encoding="utf-8")
    print(f"\nweb/fuentes/fuentes.css escrito · {total:,d} B nuevos en disco")
    return 0


if __name__ == "__main__":
    sys.exit(main())
