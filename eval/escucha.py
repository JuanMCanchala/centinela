"""Mide si el sistema OYE bien, con voz humana grabada.

Por que hace falta esto y no basta con lo que ya hay.

`eval/probar_ws.py` y `eval/conversacion_voz.py` prueban el camino de voz completo,
pero el audio lo genera Piper: es el TTS del propio sistema hablandole a su propio
STT. Eso valida la tuberia -- remuestreo, VAD, WebSocket, latencia -- y no valida lo
que de verdad importa, porque una voz sintetica es limpia, va a volumen constante y
no tiene acento. Un paciente colombiano de 68 anos con el telefono lejos de la boca
no suena asi.

Este arnes cierra ese hueco: se graban frases reales, se pasan por el STT de verdad
y se mide dos cosas distintas que se confunden con facilidad.

  1. **Que oyo** -- WER contra la frase que se dijo.
  2. **Que entendio** -- el dato clinico que sale de esa transcripcion.

La segunda es la que cuenta. Una transcripcion con dos palabras mal da igual si el
dolor sale 6; una transcripcion perfecta que no produce dato deja al agente
repreguntando, y eso el paciente lo nota.

De ahi la metrica principal: TURNOS QUE OBLIGARIAN A REPREGUNTAR. Es lo que el
paciente sufre, y es lo que hay que bajar.

Uso:

    python -m eval.escucha                      # todo lo que haya en eval/audios/
    python -m eval.escucha --guion               # imprime que hay que grabar
    python -m eval.escucha --solo dolor          # solo las fichas de un dominio

Los ficheros van en `eval/audios/` con el nombre que dice el guion. Cualquier
frecuencia de muestreo y mono o estereo: se remuestrea aqui con el mismo algoritmo
que usa el navegador. Los que falten se saltan y se avisa.
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

from centinela.clinical.normalizer import normalizar_turno  # noqa: E402
from centinela.dialog.completitud import (  # noqa: E402
    MS_CIERRE_MAXIMO,
    MS_CIERRE_MINIMO,
    respuesta_completa,
)
from centinela.stt.whisper import FRECUENCIA, WhisperSTT  # noqa: E402

DIR_AUDIOS = RAIZ / "eval" / "audios"


@dataclass
class Ficha:
    """Una grabacion esperada: que se dice, y que dato clinico debe salir."""

    archivo: str
    dice: str
    dominio: str = ""
    # Que se espera del normalizador. La clave es el atributo a comprobar.
    espera: dict = field(default_factory=dict)
    porque: str = ""


# ==========================================================================
# El guion. Cada ficha esta elegida por una razon concreta, no para rellenar.
# ==========================================================================

GUION: tuple[Ficha, ...] = (
    # --- respuestas cortas: donde el STT es mas debil y el paciente mas breve ---
    Ficha("01_si_soy_yo.wav", "Sí, soy yo",
          porque="confirmacion de identidad; 3 silabas, el turno mas corto de la llamada"),
    Ficha("02_no.wav", "No",
          porque="la respuesta mas corta posible; si esta se pierde, se pierde medio cuestionario"),
    Ficha("03_un_seis.wav", "Un seis", "dolor", {"dolor_nrs": 6},
          porque="el caso que falla de forma intermitente: 0.8s de audio con el dato clave"),
    Ficha("04_normal.wav", "Normal",
          porque="una palabra; debe entenderse pero NO marcar ningun dominio por si sola"),
    Ficha("05_como_un_tres.wav", "Como un tres", "dolor", {"dolor_nrs": 3},
          porque="numero con muletilla; 'un' no debe leerse como el numero uno"),
    Ficha("06_ocho_de_diez.wav", "Ocho de diez", "dolor", {"dolor_nrs": 8},
          porque="forma habitual de dar una escala; el 'diez' no debe capturarse como valor"),

    # --- numeros dichos en palabras: Whisper los escribe en letra a menudo ---
    Ficha("07_treinta_y_siete_cinco.wav", "Treinta y siete cinco", "fiebre",
          {"temperatura_c": 37.5},
          porque="como dice la temperatura un paciente colombiano, sin decir 'punto'"),
    Ficha("08_treinta_y_ocho_dos.wav", "Me la tomé y estaba en treinta y ocho dos", "fiebre",
          {"temperatura_c": 38.2},
          porque="temperatura en palabras dentro de una frase, y por encima del umbral rojo"),

    # --- negacion de fiebre: distinguir 'no tengo' de 'no me la he medido' ---
    Ficha("09_no_he_tenido_fiebre.wav", "No, no he tenido fiebre", "fiebre",
          {"fiebre_negada": True, "fiebre_subjetiva": False},
          porque="negar la fiebre ES responder; y no debe leerse como fiebre subjetiva"),
    Ficha("10_no_me_la_he_tomado.wav", "No me la he tomado", "fiebre",
          {"sin_termometro": True, "fiebre_negada": False},
          porque="no medirse NO es no tener fiebre; este turno debe quedar sin resolver"),

    # --- banderas rojas: las que no se pueden perder nunca ---
    Ficha("11_liquido_amarillo.wav",
          "La herida tiene un líquido amarillo espeso y huele mal", "herida",
          {"pistas.herida": "secrecion_purulenta"},
          porque="BANDERA ROJA. Si esta no se oye, el sistema falla en lo unico que no puede fallar"),
    Ficha("12_no_puedo_caminar.wav",
          "De un día para otro no puedo caminar ni apoyar el pie", "movilidad",
          {"pistas.movilidad": "incapacitante_nueva"},
          porque="BANDERA ROJA por movilidad; frase larga y con negaciones"),

    # --- regionalismos colombianos: la rubrica dice que se prueban ---
    Ficha("13_regional_axila.wav",
          "Me duele como aquí abajito de la axila hace como veinte minutos",
          porque="el ejemplo textual del README del reto; ambiguo y regional a proposito"),
    Ficha("14_maluco.wav", "Me siento maluco y destemplado", "fiebre",
          {"fiebre_subjetiva": True},
          porque="'maluco' y 'destemplado' son fiebre subjetiva en Colombia, no en un diccionario"),
    Ficha("15_rojita.wav", "La herida está un poquito rojita", "herida",
          {"pistas.herida": "eritema_leve"},
          porque="diminutivo colombiano; bandera amarilla, no roja"),

    # --- respuestas normales: para que una llamada verde cierre verde ---
    Ficha("16_camino_normal.wav", "Camino normal", "movilidad",
          {"pistas.movilidad": "normal"},
          porque="debe resolver movilidad aunque el agente estuviera preguntando otra cosa"),
    Ficha("17_como_normal.wav", "Como normal", "apetito",
          {"pistas.apetito": "normal"},
          porque="mismo caso para apetito; 'como normal' nombra su dominio"),
    Ficha("18_duermo_bien.wav", "Duermo bien", "sueno",
          {"pistas.sueno": "normal"},
          porque="cierra los seis dominios de una llamada normal"),
)


# ==========================================================================
# Utilidades
# ==========================================================================

def leer_wav(ruta: Path) -> np.ndarray:
    """WAV -> float32 mono a 16 kHz, con el mismo remuestreo que el navegador."""

    with wave.open(str(ruta), "rb") as w:
        canales = w.getnchannels()
        ancho = w.getsampwidth()
        origen = w.getframerate()
        crudo = w.readframes(w.getnframes())

    if ancho != 2:
        raise ValueError(f"{ruta.name}: se esperaba PCM de 16 bits, tiene {ancho * 8}")

    muestras = np.frombuffer(crudo, dtype="<i2").astype(np.float32) / 32768.0

    if canales > 1:
        muestras = muestras.reshape(-1, canales).mean(axis=1)

    if origen != FRECUENCIA:
        # Promediar la ventana, igual que `remuestrearA16k` en web/app.js. No es
        # el mejor remuestreo del mundo, pero es EL MISMO que el del navegador, y
        # medir con otro daria numeros que no se corresponden con la realidad.
        razon = origen / FRECUENCIA
        destino = int(len(muestras) / razon)
        salida = np.empty(destino, dtype=np.float32)
        for i in range(destino):
            desde = int(i * razon)
            hasta = min(len(muestras), int((i + 1) * razon))
            ventana = muestras[desde:hasta]
            salida[i] = float(ventana.mean()) if len(ventana) else 0.0
        muestras = salida

    return muestras


def normalizar_para_wer(texto: str) -> list[str]:
    limpio = "".join(c.lower() if c.isalnum() or c.isspace() else " " for c in texto)
    return limpio.split()


def wer(referencia: str, hipotesis: str) -> float:
    """Word Error Rate por distancia de edicion sobre palabras."""

    r = normalizar_para_wer(referencia)
    h = normalizar_para_wer(hipotesis)

    if not r:
        salida = 0.0 if not h else 1.0
    else:
        d = np.zeros((len(r) + 1, len(h) + 1), dtype=np.int32)
        d[:, 0] = np.arange(len(r) + 1)
        d[0, :] = np.arange(len(h) + 1)
        for i in range(1, len(r) + 1):
            for j in range(1, len(h) + 1):
                costo = 0 if r[i - 1] == h[j - 1] else 1
                d[i, j] = min(d[i - 1, j] + 1, d[i, j - 1] + 1, d[i - 1, j - 1] + costo)
        salida = float(d[len(r), len(h)]) / len(r)
    return salida


def valor_observado(norm, clave: str):
    """Lee del turno normalizado el atributo que pide la ficha.

    Soporta `pistas.herida` para mirar dentro del diccionario de pistas.
    """

    if clave.startswith("pistas."):
        salida = norm.pistas.get(clave.split(".", 1)[1])
    else:
        salida = getattr(norm.numeros, clave, None)
    return salida


def imprimir_guion() -> None:
    print("=" * 78)
    print("QUE HAY QUE GRABAR")
    print("=" * 78)
    print()
    print(f"Carpeta: {DIR_AUDIOS.relative_to(RAIZ)}")
    print("Formato: WAV, PCM 16 bits. Cualquier frecuencia; mono o estereo.")
    print()
    print("Como si fuera una llamada de verdad: a volumen normal, sin vocalizar de")
    print("mas, y sin cortar el audio pegado a la primera silaba -- deje medio")
    print("segundo de aire antes y despues.")
    print()
    for i, f in enumerate(GUION, 1):
        estado = "YA ESTA" if (DIR_AUDIOS / f.archivo).exists() else "falta  "
        print(f"[{estado}] {i:2d}. {f.archivo}")
        print(f'              diga: "{f.dice}"')
        if f.porque:
            print(f"              por que: {f.porque}")
        print()


# ==========================================================================
# Ejecucion
# ==========================================================================

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--guion", action="store_true", help="imprime que grabar y sale")
    ap.add_argument("--solo", default="", help="filtra por dominio o por trozo del nombre")
    ap.add_argument("--dir", default=str(DIR_AUDIOS))
    args = ap.parse_args()

    if args.guion:
        imprimir_guion()
        return 0

    carpeta = Path(args.dir)
    fichas = [
        f for f in GUION
        if not args.solo or args.solo in f.dominio or args.solo in f.archivo
    ]
    presentes = [f for f in fichas if (carpeta / f.archivo).exists()]
    faltan = [f for f in fichas if not (carpeta / f.archivo).exists()]

    codigo = 0

    if not presentes:
        print(f"No hay ni un audio en {carpeta}.")
        print("Corra `python -m eval.escucha --guion` para ver que grabar.")
        codigo = 2
    else:
        print("=" * 78)
        print("PRUEBA DE ESCUCHA -- voz humana grabada contra el STT de verdad")
        print("=" * 78)
        print()

        transcriptor = WhisperSTT()
        transcriptor.preparar()
        print(f"STT: {transcriptor.tamano} en "
              f"{transcriptor.dispositivo}/{transcriptor.tipo_computo}")
        print()

        wers: list[float] = []
        descartados: list[str] = []
        repreguntarian: list[str] = []
        datos_mal: list[str] = []
        rojas_perdidas: list[str] = []
        cierran_antes: list[str] = []
        cierran_a_medias: list[str] = []

        for f in presentes:
            muestras = leer_wav(carpeta / f.archivo)
            tr = transcriptor.transcribir(muestras)
            e = wer(f.dice, tr.texto)
            wers.append(e)

            print(f"  {f.archivo}")
            print(f'    dijo      : "{f.dice}"')
            print(f'    oyo       : "{tr.texto}"')
            print(f"    WER       : {e:.3f}   ·  {tr.duracion_audio_s}s  ·  {tr.ms:.0f} ms")
            # Los dos criterios con los que el STT decide si se queda el turno.
            # Van a la vista porque son lo primero que hay que mirar cuando un
            # audio se descarta y no se entiende por que.
            print(f"    criterios : no-voz {tr.prob_sin_voz:.2f}  ·  "
                  f"logprob {tr.logprob_medio:.2f}")

            if tr.sin_habla:
                descartados.append(f.archivo)
                repreguntarian.append(f.archivo)
                print(f"    DESCARTADO: {tr.motivo_descarte}")
            elif f.espera:
                norm = normalizar_turno(tr.texto, dominio_objetivo=f.dominio)
                fallos = []
                for clave, esperado in f.espera.items():
                    obtenido = valor_observado(norm, clave)
                    ok = obtenido == esperado
                    marca = "ok " if ok else "MAL"
                    print(f"    {marca} {clave} = {obtenido!r}  (esperado {esperado!r})")
                    if not ok:
                        fallos.append(clave)
                if fallos:
                    datos_mal.append(f.archivo)
                    repreguntarian.append(f.archivo)
                    if "ROJA" in f.porque:
                        rojas_perdidas.append(f.archivo)

            # Cierre adaptativo: con lo que el STT oyo de verdad -- no con el texto de
            # referencia --, ¿habria cerrado el turno a los 450 ms en vez de esperar
            # los 900? Se juzga sobre la transcripcion real a proposito: si el STT se
            # come una palabra, el cierre tiene que notarlo igual que lo notaria en una
            # llamada.
            if not tr.sin_habla:
                v = respuesta_completa(tr.texto, f.dominio, MS_CIERRE_MINIMO)
                if v.completa:
                    cierran_antes.append(f.archivo)
                    print(f"    cierre    : a {MS_CIERRE_MINIMO:.0f} ms · {v.motivo}")
                    if f.espera and f.archivo in datos_mal:
                        # Cerrar antes con el dato mal seria acortar el turno para
                        # quedarse con una respuesta equivocada.
                        cierran_a_medias.append(f.archivo)
                else:
                    print(f"    cierre    : espera el techo de {MS_CIERRE_MAXIMO:.0f} ms"
                          f" · {v.motivo}")
            print()

        # ------------------------------------------------------------------
        print("=" * 78)
        print("RESULTADO")
        print("=" * 78)
        n = len(presentes)
        print(f"  audios evaluados            : {n} de {len(fichas)} del guion")
        print(f"  WER medio                   : {float(np.mean(wers)):.3f}")
        print(f"  WER maximo                  : {float(np.max(wers)):.3f}")
        print(f"  descartados como 'sin voz'  : {len(descartados)}")
        print(f"  dato clinico incorrecto     : {len(datos_mal)}")
        print()
        print(f"  TURNOS QUE OBLIGARIAN A REPREGUNTAR: {len(repreguntarian)} de {n}"
              f"  ({100 * len(repreguntarian) / n:.0f} %)")
        print("      es la metrica que el paciente sufre: el agente no le entendio")
        print("      y le vuelve a preguntar lo mismo.")
        print()
        ahorro = len(cierran_antes) * (MS_CIERRE_MAXIMO - MS_CIERRE_MINIMO)
        print(f"  CIERRAN A {MS_CIERRE_MINIMO:.0f} ms EN VEZ DE {MS_CIERRE_MAXIMO:.0f}: "
              f"{len(cierran_antes)} de {n}")
        print(f"      son {MS_CIERRE_MAXIMO - MS_CIERRE_MINIMO:.0f} ms menos de pausa en")
        print(f"      cada uno; {ahorro / 1000:.1f} s en el conjunto. El resto espera el")
        print("      techo por un motivo que se imprime arriba, no por descarte.")
        if cierran_a_medias:
            print()
            print(f"  CIERRAN ANTES CON EL DATO MAL: {len(cierran_a_medias)}")
            for a in cierran_a_medias:
                print(f"      - {a}")
            print("      Acortar el turno para quedarse con una respuesta equivocada es")
            print("      peor que esperar. Esto es un fallo del cierre adaptativo.")

        if rojas_perdidas:
            print()
            print(f"  BANDERAS ROJAS PERDIDAS: {len(rojas_perdidas)}")
            for a in rojas_perdidas:
                print(f"      - {a}")
            print("      Esto es un falso negativo clinico. Es el unico fallo que")
            print("      no admite matices.")

        if faltan:
            print()
            print(f"  sin grabar todavia ({len(faltan)}):")
            for f in faltan:
                print(f"      - {f.archivo}  ->  \"{f.dice}\"")

        # El codigo de salida penaliza una bandera roja perdida y nada mas. El WER
        # de una voz concreta en una sala concreta no es un criterio de fallo; que
        # el sistema no oiga una secrecion purulenta, si.
        codigo = 1 if rojas_perdidas else 0

    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
