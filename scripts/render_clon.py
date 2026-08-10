"""Renderiza el guion con la voz clonada. Se corre a mano, fuera del venv del proyecto.

    C:\\...\\venv-voz\\Scripts\\python.exe scripts/render_clon.py --referencia ruta.wav

**No es parte del arranque.** Chatterbox corre a RTF 4 en CPU y arrastra 2.5 GB de torch, asi
que vive en un venv aparte y su salida --los WAV-- se versiona. En ejecucion, `tts/clon.py`
solo lee disco. Ver ese modulo para por que el cache va direccionado por contenido.

**La guarda de duracion, y por que hace falta.** Chatterbox es estocastico y a veces se
desboca: repite la frase o alarga el final. Medido sobre el saludo con cuatro semillas,
**dos de las cuatro salieron mal** (16.7 s y 33.9 s donde tocaban 11.6 s). Sin guarda, esas
tomas se publican y nadie las oye hasta la demo.

La referencia de cuanto debe durar cada frase no es una heuristica de caracteres: es **la
duracion que le dio Piper a la misma frase**, que ya esta en `data/audio_cache/`. Piper es
determinista y ya dijo las 60 locuciones, asi que sale gratis y es especifica de cada texto.

El clon habla mas rapido que Piper --8.5 silabas por segundo contra 7.1, medido-- asi que la
ventana no esta centrada en 1.0 sino algo por debajo. Ver `VENTANA`.

**Las muletillas van por otro camino.** Son de una palabra ("Si.", "Ya.") y ahi un modelo
autorregresivo no tiene contexto de frase: alarga o divaga. Se aplica el mismo criterio que
ya usa Piper para ellas --renderizar varias y quedarse con la mas breve-- y se informa la
duracion para poder decidir con el oido si sirven.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))

from centinela.dialog import script as S  # noqa: E402
from centinela.tts.clon import DIR_CLON, MANIFIESTO, clave_de  # noqa: E402
from centinela.tts.hablado import para_voz  # noqa: E402

CACHE_PIPER = RAIZ / "data" / "audio_cache"

# Los mandos de la variante elegida por oido entre cuatro (`sobria`), y la semilla de la toma
# elegida entre cuatro (`toma1`). Fijar la semilla es lo que permite volver a generar el
# mismo audio: sin ella cada corrida daria otra toma y el WAV versionado no seria
# reproducible.
MANDOS = {"exaggeration": 0.4, "cfg_weight": 0.4, "temperature": 0.6}
SEMILLA_ELEGIDA = 20260809
SEMILLAS_DE_REINTENTO = (20260809, 1234, 7, 99991, 424242, 31337, 8675309, 271828)

# Cuanto puede desviarse la duracion del clon de la que le dio Piper al mismo texto.
# El clon habla ~19 % mas rapido, asi que lo esperado ronda 0.84; el techo en 1.25 atrapa las
# tomas desbocadas (la peor medida fue 2.9x) sin rechazar variacion normal.
VENTANA = (0.55, 1.25)

# Por debajo de esto la razon contra Piper no dice nada: Piper articula una palabra sola en
# un cuarto de segundo y ningun modelo autorregresivo baja de ~0.7 s. Ver `con_guarda`.
MINIMO_PARA_RAZON = 1.2
TOPE_MS_CORTA = 1500

FRECUENCIA_PIPER = 22050
INTENTOS_MULETILLA = 8

# Los pacientes del desplegable de la consola (`web/app.js:105`) y los que usan los arneses,
# que NO son los mismos: el arnes dice "Ana Lucia Restrepo" sin tilde y "Mauricio Gonzalez"
# sin los dos nombres del medio. La confirmacion de identidad lleva el nombre, asi que sin
# enumerarlos la voz cambiaria de persona en el segundo turno de cada llamada.
#
# Este camino no se puede cerrar del todo y conviene decirlo: el nombre entra por la peticion,
# asi que quien escriba uno distinto oira el respaldo en esa frase --y solo en esa, porque la
# parte fija va en otra frase aparte. Ver `derivadas`.
# Los cinco procedimientos del dataset. Entran en la frase de la bandera roja con su articulo.
PROCEDIMIENTOS = (
    "Apendicectomía",
    "Colecistectomía",
    "Colectomía",
    "Reemplazo de cadera/rodilla",
    "Mastectomía",
)

NOMBRES_DE_LA_CONSOLA = (
    "Ana Lucía Restrepo",
    "Jorge Enrique Patiño",
    "Mauricio Juan González Sánchez",
    "Carmen Rosa Villalba",
    "Ana Lucia Restrepo",
    "Mauricio González",
)
# Piper deja las muletillas entre 386 y 662 ms. Una de mas de segundo y medio ya no es una
# muletilla: es el agente hablando encima del paciente.
TOPE_MS_MULETILLA = 1500


@dataclass
class Suelta:
    """Un texto que no es una locucion del guion, con la misma forma para el bucle."""

    clave: str
    texto: str
    breve: bool = False


def derivadas() -> list[tuple[str, str]]:
    """Los textos que el guion no fija pero la conversacion dice igual.

    Son cinco caminos que se sintetizan en caliente porque su texto se arma en el turno. Tres
    de ellos son **plantillas con una parte variable finita**, asi que se pueden pre-renderizar
    enumerando esa parte, y entonces la voz clonada las cubre como cualquier locucion:

      `confirmar_identidad`  lleva {nombre}, y suena en TODAS las llamadas. Cuatro pacientes
                             en la consola, cuatro locuciones. Sin esto la voz cambiaria de
                             persona en el segundo turno de cada llamada.
      `Me dice ...`          la lectura de vuelta antes de escalar (`policy.py:844`).
      `Me queda ...`         el acuse de una correccion (`policy.py:825`).

    Lo que queda fuera y se dice a proposito: la **respuesta del RAG**, que es texto abierto
    del corpus, y las lecturas de vuelta de **mas de un dominio** ("fiebre de 38.5 grados y
    liquido amarillo en la herida"), cuyo espacio es el producto de los dominios y no cabe
    enumerar. Esos dos caminos hablan con Piper, y `VozClonada.estado()` publica cuantas veces
    paso.

    La fiebre se enumera entre 36.0 y 41.0: cubre todo lo clinicamente relevante --la
    febricula empieza en 37.4-- sin renderizar decimas que nadie reporta.

    **Se renderiza por FRASE, no por fragmento.** `main.py:1620` reparte asi: un fragmento con
    clave se sintetiza entero, y uno **sin** clave pasa por `sintetizar_por_frases`, que lo
    parte con `partir_en_frases` y pide el audio de cada frase por separado. Todos estos
    textos son de los que no tienen clave, de modo que el clon se consulta frase a frase: una
    plantilla de dos frases pre-renderizada entera **no se encuentra nunca**. Medido en una
    corrida de humo antes de corregirlo -- la confirmacion de identidad aparecia como dos
    fallos, "Antes de empezar, confirmo con quien hablo." y "¿Es usted ...?", por separado.

    Partirlas ademas mejora la cobertura: la parte fija de la identidad es **una sola** entrada
    compartida por todos los pacientes, y solo la frase del nombre depende de quien llame.
    """

    from centinela.dialog import confirmacion as C
    from centinela.tts.piper import partir_en_frases

    completos = []
    for paciente in NOMBRES_DE_LA_CONSOLA:
        completos.append((
            f"identidad_{paciente.split()[0].lower()}",
            S.CONFIRMACION_IDENTIDAD.texto.format(nombre=paciente),
        ))

    frases = [C.frase_de("dolor", n) for n in range(0, 11)]
    frases += [
        C.frase_de("fiebre", round(36.0 + i * 0.1, 1)) for i in range(0, 51)
    ]
    for dominio, tabla in C._POR_CATEGORIA.items():
        frases += [C.frase_de(dominio, valor) for valor in tabla]

    for frase in [f for f in frases if f]:
        etiqueta = frase.replace(" ", "_")[:34]
        completos.append((f"dice_{etiqueta}", f"Me dice {frase}."))
        completos.append((f"queda_{etiqueta}", f"Me queda {frase}."))

    # Una entrada por frase, sin repetir las que se comparten.
    textos = []
    vistas = set()
    for etiqueta, completo in completos:
        for i, frase in enumerate(partir_en_frases(completo)):
            limpia = frase.strip()
            if limpia and limpia not in vistas:
                vistas.add(limpia)
                textos.append((f"{etiqueta}_{i}" if i else etiqueta, limpia))

    return textos


def rojas() -> list[tuple[str, str]]:
    """La frase que nombra el hallazgo al escalar, por hallazgo y por procedimiento.

        "Lo que me describe -- fiebre de 38.5 grados -- es un signo de alarma
         despues de una colecistectomia."

    Se renderiza aparte porque es la mas caras de todas y a la vez la del momento que mas
    importa. Va en el turno de la bandera roja, y ahi un cambio de voz es lo peor.

    **Lo que ya estaba cubierto y lo que no.** `CIERRE_ROJO` --la instruccion de urgencias, la
    que lleva el numero-- tiene clave y se clono con el guion. La lectura de vuelta del turno
    anterior ("Me dice fiebre de 38.5 grados") tambien. Lo unico que caia al respaldo era esta
    frase intermedia, y se vio en una corrida de humo real.

    **No se puede partir.** `partir_en_frases` la devuelve entera: los guiones no son final de
    frase. Asi que hay que enumerar el producto, y el producto es lo que decide el costo:
    los cuatro dominios rojos --fiebre desde 38.0, dolor desde 7, secrecion purulenta y
    movilidad incapacitante-- por los cinco procedimientos del dataset.
    """

    from centinela.clinical import thresholds as T
    from centinela.dialog import confirmacion as C

    hallazgos = [
        C.frase_de("fiebre", round(T.FIEBRE_ROJO_C + i * 0.1, 1))
        for i in range(0, 31)
    ]
    hallazgos += [
        C.frase_de("dolor", n) for n in range(T.DOLOR_ROJO_NRS, 11)
    ]
    hallazgos += [
        C.frase_de("herida", "secrecion_purulenta"),
        C.frase_de("movilidad", "incapacitante_nueva"),
    ]

    textos = []
    for procedimiento in PROCEDIMIENTOS:
        bajo = procedimiento.lower()
        for hallazgo in [h for h in hallazgos if h]:
            etiqueta = f"roja_{bajo.split()[0][:12]}_{hallazgo.replace(' ', '_')[:26]}"
            textos.append((
                etiqueta,
                f"Lo que me describe -- {hallazgo} -- es un signo de alarma "
                f"después de {S.articulo_de(bajo)} {bajo}.",
            ))

    return textos


def ritmo_del_clon() -> float | None:
    """Caracteres por segundo que el clon mostro en el guion ya renderizado.

    Las derivadas no tienen una locucion de Piper con la que compararse, asi que la vara sale
    del propio clon: con 60 frases medidas, su ritmo es un dato y no una suposicion.
    """

    archivo = DIR_CLON / MANIFIESTO
    tasa = None
    if archivo.exists():
        datos = json.loads(archivo.read_text(encoding="utf-8"))
        entradas = [
            e for e in (datos.get("locuciones") or {}).values()
            if e.get("s") and e.get("texto") and not e.get("muletilla")
        ]
        if entradas:
            caracteres = sum(len(para_voz(e["texto"])) for e in entradas)
            segundos = sum(e["s"] for e in entradas)
            tasa = caracteres / segundos if segundos else None
    return tasa


def guardar_manifiesto(entradas: dict, referencia: Path) -> None:
    """Escribe el manifiesto **sumando** a lo que ya hubiera.

    Se llama despues de cada locucion, no al final. La corrida completa son veinticinco
    minutos y sin esto una caida a mitad deja los WAV en disco sin registrar: existen, nadie
    los encuentra, y hay que volver a pagarlos. Sumar en vez de sobrescribir es lo que permite
    ademas renderizar el guion y las derivadas en corridas separadas.
    """

    archivo = DIR_CLON / MANIFIESTO
    previas = {}
    if archivo.exists():
        try:
            previas = (
                json.loads(archivo.read_text(encoding="utf-8")).get("locuciones") or {}
            )
        except json.JSONDecodeError:
            previas = {}

    archivo.write_text(
        json.dumps(
            {
                "voz": (
                    "AUDIOMUJER-CO clonada con Chatterbox "
                    f"(sobria, semilla {SEMILLA_ELEGIDA})"
                ),
                "motor": "chatterbox-multilingual",
                "mandos": MANDOS,
                "frecuencia": FRECUENCIA_PIPER,
                "referencia": referencia.name,
                "consentimiento": (
                    "la persona grabo la referencia para este uso; ver informe final"
                ),
                "ventana_de_duracion": list(VENTANA),
                "locuciones": {**previas, **entradas},
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def duracion(ruta: Path) -> float:
    with wave.open(str(ruta)) as w:
        segundos = w.getnframes() / w.getframerate()
    return segundos


def referencias() -> dict[str, float]:
    """Cuanto duro cada locucion en boca de Piper. Es la vara de medir."""

    medidas = {}
    for locucion in S.todas_las_locuciones() + S.naturalidad():
        archivo = CACHE_PIPER / f"{locucion.clave}.wav"
        if archivo.exists():
            medidas[locucion.clave] = duracion(archivo)
    return medidas


def escribir_wav(ruta: Path, muestras: np.ndarray, frecuencia: int) -> None:
    pcm = np.clip(muestras * 32767, -32768, 32767).astype("<i2")
    with wave.open(str(ruta), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(frecuencia)
        w.writeframes(pcm.tobytes())


def a_22050(origen: Path, destino: Path) -> bool:
    """Remuestrea con ffmpeg, que trae el filtro que hace falta.

    Chatterbox entrega a 24 kHz y Piper a 22050. El motor lee la frecuencia de cada cabecera,
    asi que un archivo de 24 kHz suena bien suelto -- pero `piper._pegar` concatena tomando
    la frecuencia de la PRIMERA pieza, de modo que una locucion pegada de trozos a dos
    frecuencias distintas sale a la velocidad equivocada. Una sola frecuencia en todo el
    cache y ese fallo no puede ocurrir.

    No se pierde nada audible: la grabacion de referencia esta limitada a 9.9 kHz por el
    codec Opus, y 22050 deja pasar hasta 11 kHz.
    """

    hecho = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(origen),
         "-ar", str(FRECUENCIA_PIPER), "-ac", "1", "-c:a", "pcm_s16le", str(destino)],
        capture_output=True, text=True,
    )
    if hecho.returncode != 0:
        print(f"      ffmpeg fallo: {hecho.stderr.strip()[:120]}")
    return hecho.returncode == 0


class Renderizador:
    def __init__(self, referencia: Path, dispositivo: str) -> None:
        import perth
        perth.PerthImplicitWatermarker = perth.DummyWatermarker

        import torch
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS as Motor

        self.torch = torch
        self.referencia = referencia
        self.modelo = Motor.from_pretrained(device=dispositivo)
        self.frecuencia = self.modelo.sr

    def una_toma(self, texto: str, semilla: int) -> tuple[np.ndarray, float]:
        self.torch.manual_seed(semilla)
        onda = self.modelo.generate(
            para_voz(texto),
            language_id="es",
            audio_prompt_path=str(self.referencia),
            **MANDOS,
        )
        muestras = np.asarray(onda).reshape(-1).astype(np.float32)
        return muestras, len(muestras) / self.frecuencia

    def con_guarda(self, texto: str, esperado: float | None) -> tuple[np.ndarray, dict]:
        """Reintenta con otra semilla hasta que la duracion sea plausible.

        **La razon contra Piper solo vale si Piper tardo lo suficiente.** Medido: Piper dice
        "Vea," en 0.26 s y el clon no baja de 0.72, o sea razon 2.76 -- y no es una toma mala,
        es que un modelo autorregresivo no articula una palabra sola en un cuarto de segundo.
        Aplicar la razon ahi rechaza las ocho semillas y gasta 84 s de computo para acabar
        aceptando la primera. Por debajo de `MINIMO_PARA_RAZON` la guarda pasa a ser un tope
        absoluto, que es lo que de verdad importa en una locucion corta: que no divague.
        """

        por_razon = esperado is not None and esperado >= MINIMO_PARA_RAZON

        def acepta(segundos: float) -> bool:
            if por_razon:
                bien = VENTANA[0] <= segundos / esperado <= VENTANA[1]
            else:
                bien = segundos * 1000 <= TOPE_MS_CORTA
            return bien

        intentos = []
        elegida = None
        for semilla in SEMILLAS_DE_REINTENTO:
            if elegida is None:
                muestras, segundos = self.una_toma(texto, semilla)
                intentos.append((semilla, segundos, muestras))
                if acepta(segundos):
                    elegida = (muestras, segundos, semilla)

        if elegida is None:
            # Ninguna cupo: se guarda la mejor de las malas y se marca. Un rechazo silencioso
            # dejaria la locucion sin audio clonado sin decirlo, y en la llamada eso se oye
            # como un cambio de hablante que nadie pidio.
            if por_razon:
                mejor = min(intentos, key=lambda i: abs(i[1] / esperado - 0.84))
            else:
                mejor = min(intentos, key=lambda i: i[1])
            elegida = (mejor[2], mejor[1], mejor[0])

        muestras, segundos, semilla = elegida
        return muestras, {
            "s": round(segundos, 2),
            "s_piper": round(esperado, 2) if esperado else None,
            "razon": round(segundos / esperado, 2) if esperado else None,
            "criterio": "razon_contra_piper" if por_razon else "tope_absoluto",
            "semilla": semilla,
            "intentos": len(intentos),
            "dentro_de_ventana": acepta(segundos),
        }

    def la_mas_breve(self, texto: str) -> tuple[np.ndarray, dict]:
        """Para las muletillas: varias tomas y se queda la mas corta."""

        mejor = None
        for semilla in SEMILLAS_DE_REINTENTO[:INTENTOS_MULETILLA]:
            muestras, segundos = self.una_toma(texto, semilla)
            if mejor is None or segundos < mejor[1]:
                mejor = (muestras, segundos, semilla)

        muestras, segundos, semilla = mejor
        return muestras, {
            "s": round(segundos, 2),
            "semilla": semilla,
            "intentos": INTENTOS_MULETILLA,
            "muletilla": True,
            "aceptable": segundos * 1000 <= TOPE_MS_MULETILLA,
        }


def main() -> int:
    partes = argparse.ArgumentParser()
    partes.add_argument("--referencia", required=True)
    partes.add_argument("--dispositivo", default="cpu")
    partes.add_argument("--solo", default="", help="claves separadas por coma")
    partes.add_argument(
        "--derivadas", action="store_true",
        help="las plantillas con parte variable finita, en vez del guion fijo",
    )
    partes.add_argument(
        "--rojas", action="store_true",
        help="la frase que nombra el hallazgo al escalar, por hallazgo y procedimiento",
    )
    partes.add_argument(
        "--rehacer", action="store_true",
        help="vuelve a renderizar lo que ya tiene WAV y entrada en el manifiesto",
    )
    opciones = partes.parse_args()

    referencia = Path(opciones.referencia)
    if not referencia.exists():
        print(f"  no existe la referencia: {referencia}")
        return 1

    if opciones.derivadas or opciones.rojas:
        tasa = ritmo_del_clon()
        if tasa is None:
            print("  falta el manifiesto del guion: corre primero el guion fijo")
            return 1
        print(f"  ritmo medido del clon: {tasa:.1f} caracteres por segundo")
        pares = rojas() if opciones.rojas else derivadas()
        locuciones = [Suelta(clave, texto) for clave, texto in pares]
        esperadas = {
            loc.clave: len(para_voz(loc.texto)) / tasa for loc in locuciones
        }
    else:
        esperadas = referencias()
        locuciones = [
            loc for loc in S.todas_las_locuciones() + S.naturalidad()
            if "{" not in loc.texto
        ]

    if opciones.solo:
        pedidas = {c for c in opciones.solo.split(",") if c}
        locuciones = [loc for loc in locuciones if loc.clave in pedidas]

    DIR_CLON.mkdir(parents=True, exist_ok=True)
    temporal = DIR_CLON / "_tmp"
    temporal.mkdir(parents=True, exist_ok=True)

    print(f"  referencia   {referencia.name}")
    print(f"  mandos       {MANDOS}")
    print(f"  locuciones   {len(locuciones)}")
    t0 = time.perf_counter()
    motor = Renderizador(referencia, opciones.dispositivo)
    print(f"  modelo listo en {time.perf_counter() - t0:.1f} s, sr={motor.frecuencia}")
    print()

    # Lo ya renderizado se salta: cada locucion cuesta entre ocho segundos y minuto y medio de
    # computo, asi que repetir una corrida entera para anadir tres frases no tiene sentido.
    # `--rehacer` fuerza. La comprobacion mira el manifiesto Y el archivo: una entrada sin WAV
    # no vale, es justo el caso que en ejecucion se oye como cambio de voz.
    ya_hechas = {}
    archivo_previo = DIR_CLON / MANIFIESTO
    if archivo_previo.exists() and not opciones.rehacer:
        ya_hechas = (
            json.loads(archivo_previo.read_text(encoding="utf-8")).get("locuciones") or {}
        )

    entradas = {}
    avisos = []
    saltadas = 0
    for i, loc in enumerate(locuciones, start=1):
        clave = clave_de(loc.texto)
        hecha = clave in ya_hechas and (DIR_CLON / f"{clave}.wav").exists()

        if hecha:
            saltadas += 1
        else:
            t = time.perf_counter()
            if getattr(loc, "breve", False):
                muestras, detalle = motor.la_mas_breve(loc.texto)
            else:
                muestras, detalle = motor.con_guarda(loc.texto, esperadas.get(loc.clave))

            crudo = temporal / f"{clave}.wav"
            escribir_wav(crudo, muestras, motor.frecuencia)
            if a_22050(crudo, DIR_CLON / f"{clave}.wav"):
                crudo.unlink(missing_ok=True)
                entradas[clave] = {
                    "clave_locucion": loc.clave, "texto": loc.texto, **detalle
                }
                guardar_manifiesto(entradas, referencia)
            else:
                avisos.append(f"{loc.clave}: no se pudo remuestrear")

            marca = ""
            if detalle.get("dentro_de_ventana") is False:
                marca = "  <-- fuera de ventana"
                avisos.append(f"{loc.clave}: razon {detalle.get('razon')} fuera de ventana")
            if detalle.get("aceptable") is False:
                marca = "  <-- muletilla demasiado larga"
                avisos.append(f"{loc.clave}: muletilla de {detalle['s']} s")

            print(f"  [{i:3d}/{len(locuciones)}] {loc.clave:34s} {detalle['s']:5.2f} s "
                  f"({time.perf_counter() - t:5.1f} s de computo){marca}", flush=True)

    guardar_manifiesto(entradas, referencia)

    fuera = [c for c, e in entradas.items() if e.get("dentro_de_ventana") is False]
    largas = [c for c, e in entradas.items() if e.get("aceptable") is False]
    print()
    print(f"  {len(entradas)} locuciones nuevas en {DIR_CLON}  ({saltadas} ya estaban)")
    print(f"  minutos de computo: {(time.perf_counter() - t0) / 60:.1f}")
    print(f"  fuera de ventana:   {len(fuera)}")
    print(f"  muletillas largas:  {len(largas)}")
    for aviso in avisos:
        print(f"  ! {aviso}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
