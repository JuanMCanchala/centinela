"""Deja la consola limpia para grabar o demostrar, sin perder nada.

El problema que resuelve es de presentacion y es real. Correr las suites deja rastro
legitimo: `make humo` abre llamadas, `make redteam` abre cuarenta y dos, y cada una que
escala produce su alerta. Tras un dia de desarrollo la bandeja tiene cientos de alertas
sin acuse -- todas correctas, todas de pruebas -- y una consola con 300 alertas rojas
fuera de plazo no se lee como un sistema que funciona: se lee como un sistema en crisis.

Lo que hace:

  1. **Respalda primero.** `sqlite3 .backup` sobre la base de llamadas, con la fecha en
     el nombre. Es seguro con el servidor corriendo, a diferencia de copiar el archivo.
  2. Vacia las tablas de operacion: llamadas, tickets, entregas, turnos, mediciones e
     incidentes.
  3. Borra las hojas de traspaso entregadas y el registro de metricas por turno.

Lo que NO toca: el indice del corpus (`data/index/`), los documentos subidos, el cache de
audio ni los modelos. Nada de eso es estado de operacion, y reconstruirlo cuesta una hora
de OCR.

Sin `--aplicar` no escribe nada: enumera lo que haria. El respaldo se hace igual antes de
borrar, asi que un descuido se deshace copiando el archivo de vuelta.

    python scripts/preparar_demo.py
    python scripts/preparar_demo.py --aplicar
    python scripts/preparar_demo.py --aplicar --fecha 2026-08-08

Despues conviene medir de nuevo sobre el estado limpio, que es como se reportan las
metricas de la rubrica:

    make up  &&  make humo  &&  make runtime  &&  make metricas
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]

# Tablas de operacion. Se vacian; no se borra el archivo, para no perder el esquema ni
# las migraciones que ya se aplicaron.
TABLAS = ("entregas", "tickets", "turnos", "mediciones", "incidentes", "llamadas")


def contar(conn: sqlite3.Connection) -> dict[str, int]:
    cuentas: dict[str, int] = {}
    for tabla in TABLAS:
        try:
            cuentas[tabla] = conn.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]
        except sqlite3.OperationalError:
            cuentas[tabla] = 0
    return cuentas


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aplicar", action="store_true", help="escribe los cambios")
    ap.add_argument("--fecha", default="", help="sufijo del respaldo (por defecto, hoy)")
    ap.add_argument("--runtime", default=str(RAIZ / "data" / "runtime"))
    args = ap.parse_args()

    runtime = Path(args.runtime)
    base = runtime / "llamadas.db"
    if not base.exists():
        print(f"no existe {base}: ya esta limpio")
        return 0

    conn = sqlite3.connect(base)
    cuentas = contar(conn)
    alertas = sorted((runtime / "alertas").glob("*.txt")) if (runtime / "alertas").exists() else []
    metricas = runtime / "metricas.jsonl"

    print("=" * 74)
    print("PREPARAR EL DEMO")
    print("=" * 74)
    print(f"  base            : {base}")
    print()
    for tabla, n in cuentas.items():
        print(f"    {tabla:12s} {n:6d} filas")
    print(f"    {'alertas':12s} {len(alertas):6d} hojas en disco")
    if metricas.exists():
        lineas = sum(1 for _ in metricas.open(encoding="utf-8"))
        print(f"    {'metricas':12s} {lineas:6d} turnos medidos")
    print()

    if not args.aplicar:
        print("  Nada escrito. Con --aplicar:")
        print("    1. se respalda la base con la fecha en el nombre,")
        print("    2. se vacian esas tablas,")
        print("    3. se borran las hojas entregadas y el registro de metricas.")
        print()
        print("  No se toca el indice del corpus, ni los documentos, ni el cache de audio.")
        conn.close()
        return 0

    # 1. Respaldo. `.backup` es seguro con el servidor corriendo.
    fecha = args.fecha or __import__("datetime").date.today().isoformat()
    destino = runtime / f"llamadas-antes-del-demo-{fecha}.db"
    respaldo = sqlite3.connect(destino)
    with respaldo:
        conn.backup(respaldo)
    respaldo.close()
    print(f"  respaldo escrito: {destino.name}  ({destino.stat().st_size / 1024:.0f} KB)")

    # 2. Vaciar. El orden importa por la clave ajena de tickets -> llamadas.
    with conn:
        for tabla in TABLAS:
            try:
                conn.execute(f"DELETE FROM {tabla}")
            except sqlite3.OperationalError as e:
                print(f"  aviso: {tabla} no se pudo vaciar ({e})")
    conn.execute("VACUUM")
    conn.close()
    print(f"  tablas vaciadas : {', '.join(TABLAS)}")

    # 3. Hojas entregadas y metricas por turno.
    for hoja in alertas:
        hoja.unlink()
    print(f"  hojas borradas  : {len(alertas)}")
    if metricas.exists():
        metricas.unlink()
        print("  metricas por turno: borradas (se regeneran con `make humo`)")

    print()
    print("  Consola limpia. Para reportar metricas sobre este estado:")
    print("    make up  &&  make humo  &&  make runtime  &&  make metricas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
