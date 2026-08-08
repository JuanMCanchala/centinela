"""Retira de la bandeja las alertas de llamadas en las que el paciente nunca hablo.

Por que existe. Cuando se anadio el cierre garantizado, el arranque encontro 445
llamadas que ningun proceso anterior habia cerrado -- restos de corridas de
`eval/redteam.py` y `eval/humo.py` que abrian una llamada y no la cerraban -- y las
cerro todas. El cierre es correcto y las llamadas tenian dominios sin responder, asi
que el motor las clasifico en AMARILLO y cada una produjo su alerta.

El resultado era correcto regla por regla y equivocado en conjunto: cien alertas
clinicas de pacientes con los que no se cruzo una palabra. A alguien con quien no se
hablo no se le puede hacer triaje.

El sistema ya no las produce: `cerrar_llamada(alertar=False)` distingue el cierre
`sin_contacto` del interrumpido, y solo el segundo genera alerta. Este script limpia
las que se crearon antes de esa distincion.

Que hace, exactamente:

  - Solo toca alertas cuya llamada tiene CERO turnos registrados. Si el paciente dijo
    aunque sea una palabra, la alerta se queda.
  - Borra el ticket, su fila de entrega y la hoja del disco.
  - Remarca la llamada como `sin_contacto`, para que quede la constancia del intento.

Sin argumentos no escribe nada: enumera lo que haria.

    python scripts/sanear_alertas_sin_contacto.py
    python scripts/sanear_alertas_sin_contacto.py --aplicar
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]

# Tres senales de que hubo conversacion, y hacen falta las tres.
#
# "cero filas en `turnos`" NO sirve por si sola: esa tabla se anadio con el cierre
# garantizado, asi que toda llamada anterior tiene cero filas ahi aunque haya tenido
# una conversacion completa. Un ensayo de este script con ese unico criterio marcaba
# para borrar los 183 tickets de la base, incluidos los 80 rojos legitimos de las
# corridas de `eval/humo.py`.
#
# `n_turnos` y `transcripcion` se escriben al cerrar y existen desde el principio, asi
# que entre los tres cubren tanto lo antiguo como lo nuevo.
CANDIDATOS = """
SELECT t.ticket_id, t.nivel, l.llamada_id, l.nombre, l.cierre_motivo, l.n_turnos
FROM tickets t
JOIN llamadas l ON l.llamada_id = t.llamada_id
WHERE (SELECT COUNT(*) FROM turnos tu WHERE tu.llamada_id = l.llamada_id) = 0
  AND COALESCE(l.n_turnos, 0) = 0
  AND COALESCE(l.transcripcion, '') = ''
  AND t.estado <> 'atendido'
ORDER BY t.creado_en
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aplicar", action="store_true", help="escribe los cambios")
    ap.add_argument("--runtime", default=str(RAIZ / "data" / "runtime"))
    args = ap.parse_args()

    runtime = Path(args.runtime)
    base = runtime / "llamadas.db"
    if not base.exists():
        print(f"no existe {base}")
        return 2

    conn = sqlite3.connect(base)
    conn.row_factory = sqlite3.Row
    filas = [dict(f) for f in conn.execute(CANDIDATOS)]

    print(f"base            : {base}")
    print(f"alertas de llamadas sin ningun turno: {len(filas)}")
    niveles: dict[str, int] = {}
    for f in filas:
        niveles[f["nivel"]] = niveles.get(f["nivel"], 0) + 1
    print(f"por nivel       : {niveles}")
    print()

    if not filas:
        print("nada que sanear")
        conn.close()
        return 0

    if not args.aplicar:
        for f in filas[:10]:
            print(f"  {f['ticket_id']:22s} {f['nivel']:9s} {f['llamada_id'][:12]}")
        if len(filas) > 10:
            print(f"  ... y {len(filas) - 10} mas")
        print()
        print("Nada escrito. Con --aplicar se borran esos tickets, sus entregas y sus")
        print("hojas, y sus llamadas quedan marcadas como sin_contacto.")
        conn.close()
        return 0

    dir_alertas = runtime / "alertas"
    hojas_borradas = 0
    for f in filas:
        hoja = dir_alertas / f"{f['ticket_id']}.txt"
        if hoja.exists():
            hoja.unlink()
            hojas_borradas += 1

    ids = [f["ticket_id"] for f in filas]
    llamadas = sorted({f["llamada_id"] for f in filas})
    marcas = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM entregas WHERE ticket_id IN ({marcas})", ids)
    conn.execute(f"DELETE FROM tickets WHERE ticket_id IN ({marcas})", ids)
    marcas_ll = ",".join("?" * len(llamadas))
    conn.execute(
        f"UPDATE llamadas SET cierre_motivo='sin_contacto', nivel_final=NULL"
        f" WHERE llamada_id IN ({marcas_ll})",
        llamadas,
    )
    conn.commit()

    print(f"tickets borrados: {len(ids)}")
    print(f"hojas borradas  : {hojas_borradas}")
    print(f"llamadas remarcadas como sin_contacto: {len(llamadas)}")
    print(f"tickets que quedan: {conn.execute('SELECT COUNT(*) FROM tickets').fetchone()[0]}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
