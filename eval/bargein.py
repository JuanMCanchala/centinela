"""Cuanto cuesta interrumpir al agente, y cuando el detector se rompe.

Interrumpir a media frase es la funcion mas facil de demostrar en una demo y la mas
facil de tener mal sin que se note. Basta bajar el umbral para que corte siempre --
tambien con una tos, tambien con el eco del propio altavoz --, o subirlo para que no
corte nunca y decir que "es robusto". Este arnes existe para que ninguna de las dos
cosas se pueda afirmar sin numero.

**El material es real y ya estaba en el repo.** Las 53 locuciones que el agente dice
de verdad (`data/audio_cache/*.wav`, sintetizadas por Piper) hacen de eco; las 18
grabaciones de voz humana de `eval/audios/` hacen de paciente que interrumpe. Nada
sintetico, ningun tono de laboratorio.

**Lo que se mide.** Se construyen mezclas de las dos cosas a una atenuacion de eco
conocida y se pasan trama a trama -- 64 ms, como el navegador -- por el MISMO
`DetectorInterrupcion` que corre en la llamada. Tres numeros:

  1. **Baches falsos por minuto de habla del agente.** Locucion sola, sin voz
     mezclada. Un bache es que el agente baje la voz 250 ms sin motivo.
  2. **Cortes falsos.** De esos baches, cuantos sobreviven a la segunda capa: la
     transcripcion del audio acumulado. Un corte falso SI pierde un turno, y tiene
     que ser cero.
  3. **Latencia y tasa de deteccion** con voz mezclada: cuanto tarda en bajar la voz
     y en callar, y en que casos no llega a detectar.

**El barrido de atenuacion es el punto.** Un detector de interrupcion siempre se
rompe a algun nivel de eco: cuando el altavoz suena tan alto que el microfono no
puede distinguir quien habla, no hay algoritmo que lo arregle sin la senal de
referencia. Lo que se publica aqui es DONDE se rompe, no la afirmacion de que no se
rompe. La atenuacion se expresa en dB respecto a la voz del paciente:

    -30 dB   auriculares, o cancelacion de eco buena
    -20 dB   portatil normal con la cancelacion del navegador
    -12 dB   altavoz a volumen medio
     -6 dB   altavoz alto: el eco es la mitad de la voz
     -3 dB   altavoz muy alto: aqui tiene que fallar, y se dice

Uso:

    python -m eval.bargein            # completo, con la segunda capa
    python -m eval.bargein --rapido   # solo la capa de energia
"""

from __future__ import annotations

import argparse
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))

from centinela.stt.bargein import (  # noqa: E402
    DetectorInterrupcion,
    Veredicto,
    rms_de,
)
from centinela.stt.whisper import FRECUENCIA, WhisperSTT  # noqa: E402

from .escucha import leer_wav  # noqa: E402

DIR_LOCUCIONES = RAIZ / "data" / "audio_cache"
DIR_VOCES = RAIZ / "eval" / "audios"

# Tramas de 1024 muestras a 16 kHz, como produce el navegador.
MUESTRAS_TRAMA = 1024
MS_TRAMA = MUESTRAS_TRAMA / FRECUENCIA * 1000.0

# Nivel al que se normaliza la voz del paciente. `web/app.js` documenta que el habla
# normal a un palmo del microfono ronda 0.05-0.25 de RMS; 0.12 es el centro.
RMS_VOZ = 0.12

# Sala tipica: piso de ruido bajo, umbral de voz en el minimo del cliente.
PISO_SALA = 0.005
UMBRAL_VOZ_SALA = 0.022

ATENUACIONES_DB = (-30.0, -20.0, -12.0, -6.0, -3.0)

# Se exige que funcione mientras el eco quede AL MENOS esto por debajo de la voz. Mas
# arriba (-6, -3 dB) es el altavoz tapando al paciente, y lo que se pide entonces es
# que se sepa, no que se arregle.
DB_EXIGIBLE = -12.0

# La voz se mezcla a partir de este punto de la locucion, para que el detector haya
# tenido tiempo de aprender el eco -- es lo que pasa en una llamada de verdad, donde
# nadie interrumpe en la primera silaba.
SEGUNDOS_ANTES_DE_INTERRUMPIR = 1.2

# Puntos de operacion del barrido. Los dos numeros deciden lo mismo -- lo alto que
# queda el umbral -- y solo tienen sentido mirados juntos.
PUNTOS = ((95.0, 2.2), (95.0, 1.8), (85.0, 2.2), (85.0, 1.8), (75.0, 1.8), (85.0, 1.5))


def se_exige(db: float) -> bool:
    """El eco queda al menos `DB_EXIGIBLE` por debajo de la voz."""

    return db <= DB_EXIGIBLE


def inicio_de_la_voz(voz: np.ndarray) -> int:
    """Primera muestra en que la grabacion tiene voz de verdad.

    Sin esto la latencia sale inflada y no es del detector: las grabaciones empiezan
    con un poco de silencio antes de que la persona hable, y medir desde el principio
    del archivo cuenta ese silencio como tiempo de reaccion.
    """

    encontrado = len(voz)
    for i in range(0, len(voz) - MUESTRAS_TRAMA, MUESTRAS_TRAMA):
        if encontrado == len(voz) and rms_de(voz[i : i + MUESTRAS_TRAMA]) > 0.04:
            encontrado = i
    return encontrado


@dataclass
class Corrida:
    """Una mezcla pasada por el detector."""

    etiqueta: str
    db: float
    con_voz: bool
    ms_hasta_bajar: float | None = None
    ms_hasta_callar: float | None = None
    baches: int = 0
    duracion_s: float = 0.0
    # Audio de cada sospecha, para que la segunda capa pueda juzgarla.
    candidatos: list[np.ndarray] = field(default_factory=list)


def normalizar(muestras: np.ndarray, objetivo: float) -> np.ndarray:
    actual = rms_de(muestras)
    if actual <= 0:
        salida = muestras
    else:
        salida = (muestras * (objetivo / actual)).astype(np.float32)
    return np.clip(salida, -1.0, 1.0)


def mezclar(eco: np.ndarray, voz: np.ndarray, desde: int) -> np.ndarray:
    """Superpone la voz sobre el eco a partir de la muestra `desde`.

    Si la voz sobresale del final de la locucion, la mezcla se alarga: en una llamada
    de verdad el agente se calla y el paciente sigue hablando.
    """

    largo = max(len(eco), desde + len(voz))
    salida = np.zeros(largo, dtype=np.float32)
    salida[: len(eco)] = eco
    salida[desde : desde + len(voz)] += voz
    return np.clip(salida, -1.0, 1.0)


def pasar_por_el_detector(
    mezcla: np.ndarray, muestra_de_la_voz: int | None, db: float, etiqueta: str,
    percentil: float | None = None, margen: float | None = None,
) -> Corrida:
    """Trama a trama, exactamente como llega en la llamada."""

    det = DetectorInterrupcion(piso_ruido=PISO_SALA, umbral_voz=UMBRAL_VOZ_SALA)
    if percentil is not None:
        det.percentil_eco = percentil
    if margen is not None:
        det.margen_eco = margen
    det.tomar_la_palabra(emite_voz=True)

    corrida = Corrida(
        etiqueta=etiqueta,
        db=db,
        con_voz=muestra_de_la_voz is not None,
        duracion_s=len(mezcla) / FRECUENCIA,
    )
    inicio_voz_ms = (
        muestra_de_la_voz / FRECUENCIA * 1000.0 if muestra_de_la_voz is not None else 0.0
    )

    acumulado = []
    ms = 0.0
    for i in range(0, len(mezcla) - MUESTRAS_TRAMA, MUESTRAS_TRAMA):
        trama = mezcla[i : i + MUESTRAS_TRAMA]
        ms += MS_TRAMA
        veredicto = det.observar(rms_de(trama), MS_TRAMA)

        if det.sospechando:
            acumulado.append(trama)

        if veredicto is Veredicto.SOSPECHA:
            corrida.baches += 1
            if corrida.ms_hasta_bajar is None:
                corrida.ms_hasta_bajar = ms - inicio_voz_ms
        elif veredicto is Veredicto.COMPROBAR:
            if corrida.ms_hasta_callar is None:
                corrida.ms_hasta_callar = ms - inicio_voz_ms
            corrida.candidatos.append(np.concatenate(acumulado) if acumulado else trama)
            acumulado = []
            # Se descarta y se sigue: en la llamada real, si no era voz el agente
            # recupera la palabra. Asi se cuentan TODOS los baches de la locucion, no
            # solo el primero.
            det.descartado()

    return corrida


def percentil(valores: list[float], p: float) -> float:
    if not valores:
        salida = 0.0
    else:
        salida = float(np.percentile(np.array(valores), p))
    return salida


def duracion_wav(ruta: Path) -> float:
    with wave.open(str(ruta), "rb") as w:
        salida = w.getnframes() / w.getframerate()
    return salida


def barrer_punto_de_operacion(audios_eco: dict, audios_voz: dict) -> None:
    """Por que el umbral esta donde esta, en vez de afirmarlo en un comentario.

    Las dos constantes que deciden -- el percentil con que se resume el eco y el margen
    con que hay que superarlo -- se mueven en el mismo sentido, asi que solo tienen
    sentido mirandolas juntas. Esto imprime la tabla.
    """

    print("  BARRIDO DEL PUNTO DE OPERACION")
    print("  percentil  margen   deteccion a -12 dB   baches/min   deteccion a -6 dB")
    print("  " + "-" * 74)

    for percentil, margen in PUNTOS:
        resultados = {}
        baches = 0
        segundos = 0.0

        for db in (-12.0, -6.0):
            factor = 10.0 ** (db / 20.0)
            detectados = 0
            for i, (nombre, voz) in enumerate(audios_voz.items()):
                eco = list(audios_eco.values())[i % len(audios_eco)]
                desde = int(SEGUNDOS_ANTES_DE_INTERRUMPIR * FRECUENCIA)
                mezcla = mezclar((eco * factor).astype(np.float32), voz, desde)
                corrida = pasar_por_el_detector(
                    mezcla, desde + inicio_de_la_voz(voz), db, nombre, percentil, margen
                )
                if corrida.ms_hasta_bajar is not None and corrida.ms_hasta_bajar >= 0:
                    detectados += 1
            resultados[db] = detectados / len(audios_voz)

            for nombre, eco in audios_eco.items():
                corrida = pasar_por_el_detector(
                    (eco * factor).astype(np.float32), None, db, nombre, percentil, margen
                )
                baches += corrida.baches
                segundos += corrida.duracion_s

        por_minuto = baches / (segundos / 60.0) if segundos else 0.0
        elegido = " <- el que corre" if (percentil, margen) == (85.0, 1.8) else ""
        print(f"  {percentil:8.0f}  {margen:6.1f}   {resultados[-12.0] * 100:15.0f} %   "
              f"{por_minuto:10.2f}   {resultados[-6.0] * 100:15.0f} %{elegido}")

    print()
    print("  El intercambio, dicho como es: bajar el umbral compra deteccion con altavoz")
    print("  alto y paga en baches. (85, 2.2) no da ni un bache pero solo detecta el 42 %")
    print("  a -6 dB; (85, 1.8) detecta el doble y cuesta 0.23 baches por minuto de habla")
    print("  del agente -- un bajon de 250 ms cada cuatro minutos, y solo si el altavoz")
    print("  esta alto. Se elige ese porque un bache es casi imperceptible y no perder la")
    print("  interrupcion no lo es. Bajar mas (75, o margen 1.5) cuadruplica los baches")
    print("  para ganar poco.")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rapido", action="store_true",
                    help="solo la capa de energia, sin confirmar con el STT")
    ap.add_argument("--barrido", action="store_true",
                    help="tabla del punto de operacion (percentil x margen)")
    ap.add_argument("--voces", type=int, default=12,
                    help="cuantas grabaciones humanas se usan por atenuacion")
    args = ap.parse_args()

    locuciones = sorted(DIR_LOCUCIONES.glob("*.wav"))
    voces = sorted(DIR_VOCES.glob("*.wav"))

    if not locuciones:
        print(f"no hay locuciones en {DIR_LOCUCIONES}. Corre `make piper` y arranca la API.")
        return 2
    if not voces:
        print(f"no hay grabaciones humanas en {DIR_VOCES}. Corre `make escucha-guion`.")
        return 2

    print("=" * 78)
    print("BARGE-IN: cuanto cuesta interrumpir al agente, y donde se rompe")
    print("=" * 78)
    print(f"  eco       : {len(locuciones)} locuciones reales del agente ({DIR_LOCUCIONES.name}/)")
    print(f"  voz       : {len(voces)} grabaciones humanas ({DIR_VOCES.name}/)")
    print(f"  sala      : piso {PISO_SALA} · umbral de voz {UMBRAL_VOZ_SALA}")
    print(f"  trama     : {MUESTRAS_TRAMA} muestras ({MS_TRAMA:.0f} ms), como el navegador")
    print()

    audios_eco = {p.stem: normalizar(leer_wav(p), RMS_VOZ) for p in locuciones}
    audios_voz = {p.stem: normalizar(leer_wav(p), RMS_VOZ) for p in voces[: args.voces]}

    if args.barrido:
        barrer_punto_de_operacion(audios_eco, audios_voz)

    stt = None
    if not args.rapido:
        print("  cargando el STT para la segunda capa...", flush=True)
        stt = WhisperSTT(dir_modelos=RAIZ / "data" / "modelos" / "whisper")
        stt.calentar()
        print()

    filas = []
    cortes_falsos_exigibles = 0
    sin_detectar_exigibles = []

    for db in ATENUACIONES_DB:
        factor = 10.0 ** (db / 20.0)

        # --- 1. Locucion sola: todo lo que dispare aqui es falso ---------------
        baches = 0
        segundos_agente = 0.0
        cortes_falsos = 0

        for nombre, eco in audios_eco.items():
            corrida = pasar_por_el_detector(
                (eco * factor).astype(np.float32), None, db, nombre
            )
            baches += corrida.baches
            segundos_agente += corrida.duracion_s

            if stt is not None:
                for candidato in corrida.candidatos:
                    if len(candidato) / FRECUENCIA >= 0.2:
                        trans = stt.transcribir(candidato)
                        if not trans.sin_habla:
                            cortes_falsos += 1
                            print(f"    CORTE FALSO en {nombre} a {db:.0f} dB: "
                                  f"{trans.texto[:60]!r}")

        # --- 2. Con voz humana encima: tiene que detectar ----------------------
        latencias_bajar = []
        latencias_callar = []
        detectados = 0
        total = 0

        for i, (nombre, voz) in enumerate(audios_voz.items()):
            eco = list(audios_eco.values())[i % len(audios_eco)]
            desde = int(SEGUNDOS_ANTES_DE_INTERRUMPIR * FRECUENCIA)
            mezcla = mezclar((eco * factor).astype(np.float32), voz, desde)
            # La referencia de latencia es la primera muestra con voz de verdad, no la
            # primera del archivo: lo que se mide es la reaccion del detector.
            corrida = pasar_por_el_detector(mezcla, desde + inicio_de_la_voz(voz), db, nombre)
            total += 1

            if corrida.ms_hasta_bajar is not None and corrida.ms_hasta_bajar >= 0:
                detectados += 1
                latencias_bajar.append(corrida.ms_hasta_bajar)
                if corrida.ms_hasta_callar is not None:
                    latencias_callar.append(corrida.ms_hasta_callar)
            elif se_exige(db):
                sin_detectar_exigibles.append(f"{nombre} a {db:.0f} dB")

        por_minuto = baches / (segundos_agente / 60.0) if segundos_agente else 0.0
        if se_exige(db):
            cortes_falsos_exigibles += cortes_falsos

        filas.append({
            "db": db,
            "baches_min": por_minuto,
            "baches": baches,
            "cortes_falsos": cortes_falsos,
            "deteccion": detectados / total if total else 0.0,
            "p50_bajar": percentil(latencias_bajar, 50),
            "p95_bajar": percentil(latencias_bajar, 95),
            "p50_callar": percentil(latencias_callar, 50),
            "p95_callar": percentil(latencias_callar, 95),
        })

    # ------------------------------------------------------------------ informe
    print("  atenuacion   baches/min  cortes falsos   deteccion   bajar p50/p95   callar p50")
    print("  " + "-" * 76)
    for f in filas:
        marca = " " if se_exige(f["db"]) else "~"
        print(f"  {marca}{f['db']:6.0f} dB   {f['baches_min']:9.2f}   "
              f"{f['cortes_falsos']:11d}   {f['deteccion'] * 100:7.0f} %   "
              f"{f['p50_bajar']:5.0f}/{f['p95_bajar']:4.0f} ms   "
              f"{f['p50_callar']:6.0f} ms")
    print()
    print(f"  Se exige hasta {DB_EXIGIBLE:.0f} dB. Las filas marcadas con ~ son el altavoz")
    print("  tapando al paciente: se publican para que se sepa donde esta el limite.")
    print()

    if args.rapido:
        print("  [--rapido] La columna de cortes falsos no se midio: hace falta el STT.")
        print("  Un bache cuesta 250 ms de voz baja; solo un CORTE pierde el turno.")
        codigo = 0
    else:
        print("  Un bache baja la voz 250 ms y el agente sigue. Solo un corte falso")
        print("  pierde el turno, y por eso es el unico numero que tiene que ser cero.")
        print()

        if cortes_falsos_exigibles:
            print(f"  FALLO: {cortes_falsos_exigibles} corte(s) falso(s) por encima de "
                  f"{DB_EXIGIBLE:.0f} dB.")
            codigo = 1
        elif sin_detectar_exigibles:
            print(f"  FALLO: no detecto la voz en {len(sin_detectar_exigibles)} caso(s) "
                  f"exigibles: {', '.join(sin_detectar_exigibles[:6])}")
            codigo = 1
        else:
            print(f"  OK: 0 cortes falsos y 100 % de deteccion hasta {DB_EXIGIBLE:.0f} dB.")
            codigo = 0

    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
