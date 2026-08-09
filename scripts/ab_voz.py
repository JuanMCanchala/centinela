"""Compara las voces de Piper que ya están en disco, sobre las frases de la llamada.

La voz actual se eligió por **latencia**, y el razonamiento está escrito en
`scripts/fetch_piper.py`: las variantes `high` de Piper corren a un factor de tiempo real
cercano a 1.0 en CPU —tardan en generar un segundo de audio lo que ese segundo dura— y eso
es inservible en una conversación. Se prefirió `medium`, y dentro de `medium` el acento
latinoamericano.

**Ese cálculo cambió, y no por una medición de voz.** Al medir la latencia por camino
apareció que **el 83 % de los turnos se sirven del caché de audio pre-renderizado**: son
locuciones del guion, que se conocen antes de que suene el teléfono. Para esas, el factor de
tiempo real **no cuesta nada** — se pagan una vez, al arrancar. Solo el 17 % restante
sintetiza en el turno, y solo ahí el RTF se convierte en latencia que el paciente siente.

Así que la pregunta ya no es «¿cuál es la voz más rápida?» sino **«¿cuánto cuesta de verdad
la voz más natural, y dónde?»**. Esto lo mide:

  arranque         cargar el modelo, una vez por proceso
  RTF              segundos de cómputo por segundo de audio, en el camino EN VIVO
  primera frase    lo que el paciente espera antes de oír algo, que es la cifra de la rúbrica
  pre-render       lo que costaría llenar el caché entero una vez

Lo que este script **no** hace es juzgar el timbre. No se puede medir con un número, así que
escribe los WAV de las mismas frases con cada voz en `data/ab_voz/` para que se escuchen al
lado. La decisión de timbre es de quien oye.

    python scripts/ab_voz.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))

from centinela.dialog import script as S  # noqa: E402
from centinela.tts.piper import DIR_PIPER, PiperTTS  # noqa: E402

DESTINO = RAIZ / "docs" / "metrics" / "ab_voz.json"
DIR_MUESTRAS = RAIZ / "data" / "ab_voz"

# Las frases con las que se compara. Elegidas por lo que representan en la llamada, no por
# bonitas: la que abre, la más larga y crítica, una pregunta del guion, y la muletilla que
# suena en cada turno.
FRASES = (
    ("saludo", S.SALUDO.texto),
    ("cierre_rojo", S.CIERRE_ROJO.texto),
    ("pregunta_dolor", S.PREGUNTA_POR_DOMINIO["dolor"].inicial.texto),
    ("silencio_acompanar", S.SILENCIO_ACOMPANAR.texto),
)


async def medir(nombre: str, ruta_voz: Path) -> dict:
    salida = DIR_MUESTRAS / nombre
    salida.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    tts = PiperTTS(voz=ruta_voz, dir_cache=salida)
    # La primera síntesis paga el arranque del proceso residente y la carga del modelo.
    primera = await tts.sintetizar(FRASES[0][1])
    ms_arranque = (time.perf_counter() - t0) * 1000.0

    resultados = []
    try:
        for clave, texto in FRASES:
            t = time.perf_counter()
            audio = await tts.sintetizar(texto)
            ms = (time.perf_counter() - t) * 1000.0
            segundos_audio = audio.duracion_s if audio.wav else 0.0
            (salida / f"{clave}.wav").write_bytes(audio.wav or b"")
            resultados.append({
                "frase": clave,
                "caracteres": len(texto),
                "segundos_audio": round(segundos_audio, 2),
                "ms_sintesis": round(ms, 1),
                "rtf": round(ms / 1000.0 / segundos_audio, 3) if segundos_audio else None,
            })

        # La cifra de la rúbrica: lo que el paciente espera antes de oír algo. Se mide con
        # la síntesis por frases, que es como sale en la llamada -- no con la locución
        # entera, que es lo que engañaría.
        t = time.perf_counter()
        ms_primera_frase = None
        async for _frase, _audio in tts.sintetizar_por_frases(S.CIERRE_ROJO.texto):
            ms_primera_frase = round((time.perf_counter() - t) * 1000.0, 1)
            break
    finally:
        await tts.cerrar()

    con_rtf = [r["rtf"] for r in resultados if r["rtf"]]
    audio_total = sum(r["segundos_audio"] for r in resultados)
    computo_total = sum(r["ms_sintesis"] for r in resultados) / 1000.0

    return {
        "voz": nombre,
        "mb_modelo": round(ruta_voz.stat().st_size / 1024 / 1024, 1),
        "ms_arranque": round(ms_arranque, 1),
        "rtf_medio": round(computo_total / audio_total, 3) if audio_total else None,
        "rtf_peor": max(con_rtf) if con_rtf else None,
        "ms_primera_frase_cierre_rojo": ms_primera_frase,
        "segundos_audio_de_las_frases": round(audio_total, 2),
        "por_frase": resultados,
        "muestras_en": str(salida.relative_to(RAIZ)),
        "_primera_ok": bool(primera.wav),
    }


async def correr(voces: list[tuple[str, Path]]) -> dict:
    medidas = []
    for nombre, ruta in voces:
        print(f"  midiendo {nombre}...")
        medidas.append(await medir(nombre, ruta))

    informe = {
        "por_que": (
            "la voz se eligio por latencia en CPU, cuando no se sabia que el 83 % de los "
            "turnos se sirven del cache pre-renderizado. Para esos el factor de tiempo "
            "real no cuesta nada: se paga una vez al arrancar. La pregunta pasa a ser "
            "cuanto cuesta la voz mas natural y donde."
        ),
        "lo_que_no_mide": (
            "el timbre. No se puede poner en un numero, asi que se escriben los WAV de las "
            "mismas frases con cada voz para escucharlos al lado."
        ),
        "frases": [c for c, _ in FRASES],
        "medidas": medidas,
    }

    DESTINO.parent.mkdir(parents=True, exist_ok=True)
    DESTINO.write_text(
        json.dumps(informe, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return informe


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voces", nargs="*", default=None,
                    help="nombres de voz sin extension; por omision todas las de disco")
    a = ap.parse_args()

    dir_voces = DIR_PIPER / "voces"
    disponibles = sorted(dir_voces.glob("*.onnx"))
    if a.voces:
        disponibles = [v for v in disponibles if v.stem in a.voces]

    if not disponibles:
        print("No hay voces en disco. Corra `make piper`.")
        codigo = 1
    else:
        informe = asyncio.run(correr([(v.stem, v) for v in disponibles]))

        print()
        print("=" * 92)
        print("A/B DE VOZ - Piper, las voces que ya estan en disco")
        print("=" * 92)
        print()
        cab = f"  {'voz':28s}{'MB':>6s}{'arranque':>10s}{'RTF':>8s}{'RTF peor':>10s}"
        print(cab + f"{'1a frase':>10s}")
        print("  " + "-" * 88)
        for m in informe["medidas"]:
            print(f"  {m['voz']:28s}{m['mb_modelo']:>6.1f}"
                  f"{m['ms_arranque']:>9.0f}ms"
                  f"{m['rtf_medio'] if m['rtf_medio'] else 0:>8.3f}"
                  f"{m['rtf_peor'] if m['rtf_peor'] else 0:>10.3f}"
                  f"{m['ms_primera_frase_cierre_rojo'] or 0:>8.0f}ms")
        print()
        print("  RTF = segundos de computo por segundo de audio. Solo cuenta en el 17 % de")
        print("  turnos que sintetizan en vivo; el 83 % sale del cache pre-renderizado.")
        print("  '1a frase' es lo que el paciente espera antes de oir algo: la cifra de la")
        print("  rubrica, medida sobre la locucion mas larga del sistema.")
        print()
        print(f"  muestras para escuchar en {DIR_MUESTRAS.relative_to(RAIZ)}/<voz>/")
        print(f"  informe en {DESTINO.relative_to(RAIZ)}")
        codigo = 0
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
