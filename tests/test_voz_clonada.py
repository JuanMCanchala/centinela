"""La voz clonada se lee del disco, y cuando falta se oye y se cuenta.

La voz del agente es una clonación de una grabación humana con consentimiento, hecha con
Chatterbox **fuera del proceso**: el modelo corre a RTF 4 en CPU y arrastra 2.5 GB de torch,
así que sintetizar en la llamada es imposible y meterlo en el venv de ejecución reventaría la
compuerta G2 de quince minutos. Lo que queda en ejecución es un lector de WAV.

Eso deja un riesgo que estas pruebas cubren: **el respaldo es otra voz**. Si el clon no tiene
la frase, habla Piper, y el paciente oye cambiar de persona a mitad de llamada. No es un
error —la llamada sigue— pero es de las cosas que más delatan a una máquina, así que no puede
ocurrir en silencio: cada fallo se cuenta, se registra con su texto, y sale en `estado()`
para que se vea en la consola y en las métricas.

El cache va **direccionado por contenido**: la clave es el hash del texto ya normalizado por
`para_voz`, no el nombre de la locución. Es lo que permite cubrir texto sin clave —una
respuesta del RAG, una lectura de vuelta— y es también lo que hace que editar el guion
provoque un fallo audible en vez de servir audio viejo con texto nuevo, que es el mismo
defecto que hubo que corregir una vez en `PiperTTS._firma`.
"""

from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))

from centinela.tts.clon import VozClonada, clave_de  # noqa: E402
from centinela.tts.hablado import para_voz  # noqa: E402
from centinela.tts.piper import PiperTTS  # noqa: E402


def _wav(ruta: Path, segundos: float = 0.1, frecuencia: int = 22050) -> bytes:
    with wave.open(str(ruta), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(frecuencia)
        w.writeframes(b"\x00\x01" * int(frecuencia * segundos))
    return ruta.read_bytes()


def _montar(tmp_path: Path, textos: dict[str, str]) -> VozClonada:
    """Un directorio de clon con los textos dados ya renderizados."""

    entradas = {}
    for clave_loc, texto in textos.items():
        clave = clave_de(texto)
        _wav(tmp_path / f"{clave}.wav")
        entradas[clave] = {"clave_locucion": clave_loc, "texto": texto, "s": 0.1}
    (tmp_path / "manifiesto.json").write_text(
        json.dumps({"voz": "prueba", "locuciones": entradas}, ensure_ascii=False),
        encoding="utf-8",
    )
    return VozClonada(directorio=tmp_path)


# ----------------------------------------------------------------------
# La clave


def test_la_clave_es_del_texto_hablado_no_del_escrito():
    """"38.5 °C" y "38 y medio grados" suenan igual: un solo WAV los sirve.

    Si la clave se calculara sobre el texto crudo harían falta dos archivos para el mismo
    audio, y el segundo nunca se encontraría.
    """

    escrito = "Tiene fiebre de 38.5 °C."
    hablado = para_voz(escrito)

    assert hablado != escrito
    assert clave_de(escrito) == clave_de(hablado)


def test_la_clave_cambia_si_cambia_el_texto():
    """Editar el guion tiene que provocar un fallo, no servir el audio viejo."""

    assert clave_de("Buenos días.") != clave_de("Buenas tardes.")


# ----------------------------------------------------------------------
# Aciertos y fallos


def test_un_acierto_devuelve_el_wav_y_se_cuenta(tmp_path):
    clon = _montar(tmp_path, {"saludo": "Buenos días."})

    datos = clon.buscar("Buenos días.")

    assert datos is not None
    assert datos.startswith(b"RIFF")
    assert (clon.aciertos, clon.fallos) == (1, 0)


def test_un_fallo_no_revienta_y_guarda_el_texto(tmp_path):
    """Sin el texto no se puede volver a renderizar lo que faltó."""

    clon = _montar(tmp_path, {"saludo": "Buenos días."})

    assert clon.buscar("Una frase que nadie pre-renderizó.") is None
    assert clon.fallos == 1
    assert clon.textos_sin_clonar == ["Una frase que nadie pre-renderizó."]


def test_el_mismo_fallo_no_se_apunta_dos_veces(tmp_path):
    clon = _montar(tmp_path, {})

    clon.buscar("Falta esta.")
    clon.buscar("Falta esta.")

    assert clon.fallos == 2
    assert clon.textos_sin_clonar == ["Falta esta."]


def test_una_entrada_sin_archivo_es_un_fallo_no_una_excepcion(tmp_path):
    """El manifiesto puede quedar por delante de los WAV: se cae al respaldo, no al suelo."""

    clon = _montar(tmp_path, {"saludo": "Buenos días."})
    (tmp_path / f"{clave_de('Buenos días.')}.wav").unlink()

    assert clon.buscar("Buenos días.") is None
    assert clon.fallos == 1


def test_un_manifiesto_ilegible_deja_el_clon_apagado(tmp_path):
    """Un JSON roto no puede impedir que el agente hable."""

    (tmp_path / "manifiesto.json").write_text("{esto no es json", encoding="utf-8")
    clon = VozClonada(directorio=tmp_path)

    assert clon.disponible is False
    assert clon.buscar("Buenos días.") is None


def test_sin_directorio_el_clon_no_esta_disponible(tmp_path):
    clon = VozClonada(directorio=tmp_path / "no_existe")

    assert clon.disponible is False


# ----------------------------------------------------------------------
# El estado, que es lo que se publica


def test_el_estado_publica_la_cobertura_y_que_el_fallo_cambia_de_voz(tmp_path):
    clon = _montar(tmp_path, {"saludo": "Buenos días."})
    clon.buscar("Buenos días.")
    clon.buscar("Buenos días.")
    clon.buscar("Otra cosa.")

    estado = clon.estado()

    assert estado["aciertos"] == 2
    assert estado["fallos"] == 1
    assert estado["cobertura_pct"] == pytest.approx(66.7, abs=0.1)
    assert "cambia de voz" in estado["al_fallar"]
    assert estado["textos_sin_clonar"] == ["Otra cosa."]


def test_sin_peticiones_la_cobertura_es_desconocida_no_cero(tmp_path):
    """Cero de cero no es 0 %: es que todavía no se ha pedido nada."""

    assert _montar(tmp_path, {}).estado()["cobertura_pct"] is None


# ----------------------------------------------------------------------
# El motor consulta el clon antes de sintetizar


@pytest.mark.asyncio
async def test_el_motor_sirve_el_clon_sin_tocar_piper(tmp_path):
    clon = _montar(tmp_path, {"saludo": "Buenos días."})
    tts = PiperTTS(dir_cache=tmp_path / "cache", clon=clon)

    llamadas = []

    async def no_deberia(*a, **k):
        llamadas.append(a)
        raise AssertionError("Piper no debería sintetizar lo que el clon ya tiene")

    tts._sintetizar_con_piper = no_deberia

    audio = await tts.sintetizar("Buenos días.", clave="saludo")

    assert audio.wav.startswith(b"RIFF")
    assert audio.desde_cache is True
    assert llamadas == []


@pytest.mark.asyncio
async def test_el_prerenderizado_no_se_conforma_con_el_clon(tmp_path):
    """`usar_clon=False` mantiene caliente el cache de Piper, que es la red de seguridad.

    Si el pre-renderizado aceptara el acierto del clon, la red no se tejería y un WAV clonado
    perdido acabaría sintetizando en vivo en mitad de una llamada.
    """

    clon = _montar(tmp_path, {"saludo": "Buenos días."})
    tts = PiperTTS(dir_cache=tmp_path / "cache", clon=clon)

    pedidos = []

    async def anotar(texto, clave, t0):
        pedidos.append(texto)
        return type("A", (), {"wav": b"RIFFfalso", "desde_cache": False})()

    tts._sintetizar_con_piper = anotar

    await tts.sintetizar("Buenos días.", clave="saludo", usar_clon=False)

    assert pedidos == ["Buenos días."]
    # El clon no se consultó, así que no cuenta ni acierto ni fallo.
    assert (clon.aciertos, clon.fallos) == (0, 0)


@pytest.mark.asyncio
async def test_sin_clon_el_motor_se_comporta_como_siempre(tmp_path):
    tts = PiperTTS(dir_cache=tmp_path / "cache", clon=None)

    pedidos = []

    async def anotar(texto, clave, t0):
        pedidos.append(texto)
        return type("A", (), {"wav": b"RIFFfalso", "desde_cache": False})()

    tts._sintetizar_con_piper = anotar

    await tts.sintetizar("Buenos días.", clave="saludo")

    assert pedidos == ["Buenos días."]
    assert tts.estado()["voz_clonada"] is None
