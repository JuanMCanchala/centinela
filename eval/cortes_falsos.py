"""Cortes falsos del cierre adaptativo: cuando el agente le corta la frase al paciente.

El mercado de agentes de voz publica esta cifra como **false cutoff rate**, y es el eje en
el que se comparan los detectores de fin de turno: LiveKit reporta 9.9 % de cortes falsos
con un presupuesto de 300 ms, frente al 27.7 % de un VAD acústico. Aquí no hay un modelo
aprendido — el cierre lo decide `dialog/completitud.py`, que es código con conocimiento del
dominio — así que la comparación honesta empieza por medir lo mismo que ellos.

**La medición no necesita audio nuevo, y esa es la parte bonita.** Una pausa a mitad de
frase deja al servidor con un **prefijo** de la respuesta: es exactamente lo que ve si el
paciente dice «como un seis…» y calla medio segundo antes de seguir con «…pero por las
noches me sube». Así que basta con recorrer todos los prefijos de cada respuesta del guion
y preguntarle al cierre qué haría en cada uno.

**Y la cifra que importa no es la del mercado, es una más estricta.** Cerrar antes de
tiempo no siempre cuesta algo: si el paciente ya dijo «como un seis» y lo que faltaba era
«…de dolor», el dato clínico es el mismo y los 450 ms ahorrados son gratis. El corte falso
que duele es el que **cambia el dato clínico**, y eso se comprueba normalizando el prefijo
y la frase completa y comparando lo que sale. Tres números, entonces:

    cierra en el prefijo         cuantas respuestas cierran antes de acabar
    y el dato no cambia          el ahorro gratis
    y el dato SI cambia          el corte falso con costo clinico   <-- tiene que ser 0

    python -m eval.cortes_falsos
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))
sys.path.insert(0, str(RAIZ))

from centinela.clinical.normalizer import normalizar_turno  # noqa: E402
from centinela.dialog.completitud import (  # noqa: E402
    MS_CIERRE_MAXIMO,
    MS_CIERRE_MINIMO,
    respuesta_completa,
)

from eval.escucha import GUION, valor_observado  # noqa: E402

DESTINO = RAIZ / "docs" / "metrics" / "cortes_falsos.json"

# Los atributos clinicos que se comparan entre el prefijo y la frase completa. Son los
# que el motor de triaje mira, asi que un cambio en cualquiera de ellos puede mover el
# nivel de la llamada.
ATRIBUTOS = (
    "dolor_nrs", "temperatura_c", "fiebre_negada", "sin_termometro",
    "pista_herida", "pista_movilidad", "pista_apetito", "pista_sueno",
)


def _clinico(texto: str, dominio: str) -> dict:
    """Lo que el sistema extraeria de este texto. Con lo que se compara el prefijo."""

    norm = normalizar_turno(texto, dominio_objetivo=dominio)
    salida = {}
    for clave in ATRIBUTOS:
        try:
            salida[clave] = valor_observado(norm, clave)
        except Exception:  # noqa: BLE001
            salida[clave] = None
    return salida


def _primer_prefijo_que_cierra(dice: str, dominio: str) -> tuple[int, str]:
    """El prefijo mas corto que el cierre adaptativo daria por completo.

    Devuelve (numero de palabras, texto). `(0, "")` si ninguno cierra: entonces el turno
    espera el techo, que es la conducta conservadora.
    """

    palabras = dice.split()
    encontrado = (0, "")
    for corte in range(1, len(palabras)):
        prefijo = " ".join(palabras[:corte])
        if respuesta_completa(prefijo, dominio, MS_CIERRE_MINIMO).completa:
            encontrado = (corte, prefijo)
            break
    return encontrado


def correr() -> dict:
    t0 = time.perf_counter()
    filas: list[dict] = []

    for f in GUION:
        if f.dominio:
            palabras = f.dice.split()
            corte, prefijo = _primer_prefijo_que_cierra(f.dice, f.dominio)
            cierra_antes = corte > 0

            if cierra_antes:
                antes = _clinico(prefijo, f.dominio)
                completo = _clinico(f.dice, f.dominio)
                cambian = {
                    k: {"con_el_prefijo": antes[k], "con_la_frase_entera": completo[k]}
                    for k in ATRIBUTOS
                    if antes[k] != completo[k]
                }
            else:
                cambian = {}

            filas.append({
                "archivo": f.archivo,
                "dominio": f.dominio,
                "dice": f.dice,
                "palabras": len(palabras),
                "cierra_en_la_palabra": corte or None,
                "prefijo_que_cierra": prefijo or None,
                "el_dato_cambia": bool(cambian),
                "que_cambia": cambian,
            })

    con_dominio = len(filas)
    cortan = [f for f in filas if f["cierra_en_la_palabra"]]
    con_costo = [f for f in cortan if f["el_dato_cambia"]]

    informe = {
        "n_respuestas_con_dominio": con_dominio,
        "cierran_en_un_prefijo": len(cortan),
        "de_esas_sin_costo_clinico": len(cortan) - len(con_costo),
        "cortes_falsos_con_costo_clinico": len(con_costo),
        "tasa_cortes_con_costo": (
            round(len(con_costo) / con_dominio, 4) if con_dominio else 0.0
        ),
        "ms_piso": MS_CIERRE_MINIMO,
        "ms_techo": MS_CIERRE_MAXIMO,
        "definicion": (
            "una pausa a mitad de frase deja al servidor con un prefijo de la respuesta. "
            "Se recorren todos los prefijos y se pregunta al cierre adaptativo que haria "
            "en cada uno. El corte solo cuenta como fallo si el dato clinico que sale del "
            "prefijo es distinto del que sale de la frase entera."
        ),
        "por_que_no_es_la_cifra_del_mercado": (
            "el mercado publica la tasa de cortes falsos a secas. Aqui se separa el corte "
            "que no cuesta nada -- el paciente ya dijo el numero y lo que faltaba era "
            "relleno -- del que cambia el dato clinico. El primero es el ahorro que se "
            "buscaba; el segundo es el unico que hay que llevar a cero."
        ),
        "respuestas": filas,
        "segundos": round(time.perf_counter() - t0, 2),
    }

    DESTINO.parent.mkdir(parents=True, exist_ok=True)
    DESTINO.write_text(
        json.dumps(informe, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return informe


def main() -> int:
    informe = correr()

    print("=" * 78)
    print("CORTES FALSOS DEL CIERRE ADAPTATIVO")
    print("=" * 78)
    print()

    for f in informe["respuestas"]:
        if f["cierra_en_la_palabra"]:
            marca = "COSTO " if f["el_dato_cambia"] else "gratis"
            print(f"  {marca} {f['archivo'][:34]:34s} cierra en {f['cierra_en_la_palabra']}"
                  f"/{f['palabras']} palabras")
            print(f"         «{f['prefijo_que_cierra']}»  de  «{f['dice']}»")
            for clave, v in f["que_cambia"].items():
                print(f"           {clave}: {v['con_el_prefijo']!r} en vez de "
                      f"{v['con_la_frase_entera']!r}")
        else:
            print(f"  espera {f['archivo'][:34]:34s} ningun prefijo cierra; "
                  f"llega al techo de {informe['ms_techo']:.0f} ms")
    print()
    print("-" * 78)
    print(f"  respuestas con dominio abierto : {informe['n_respuestas_con_dominio']}")
    print(f"  cierran en un prefijo          : {informe['cierran_en_un_prefijo']}")
    print(f"    sin costo clinico            : {informe['de_esas_sin_costo_clinico']}"
          "   (el ahorro que se buscaba)")
    print(f"  CORTES CON COSTO CLINICO       : "
          f"{informe['cortes_falsos_con_costo_clinico']}   <-- tiene que ser 0")
    print(f"  tasa                           : "
          f"{100 * informe['tasa_cortes_con_costo']:.1f} %")
    print(f"  {informe['segundos']}s")
    print()
    print(f"  informe en {DESTINO.relative_to(RAIZ)}")

    if informe["cortes_falsos_con_costo_clinico"]:
        print()
        print("  FALLA: el cierre adaptativo se queda con un dato distinto del que el")
        print("  paciente acabo diciendo. Esperar el techo es mejor que eso.")
        codigo = 1
    else:
        print()
        print("  Ningun cierre anticipado cambia el dato clinico.")
        codigo = 0
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
