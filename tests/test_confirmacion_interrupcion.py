"""La confirmacion de una interrupcion no se rinde con una ventana demasiado corta.

**Como aparecio.** Al mover el STT a la GPU, `medium` sustituyo a `small` y una sola
comprobacion de todo el proyecto se rompio: la de `eval/humo.py` que exige que el
servidor mande `callar` cuando confirma que era el paciente. Resulto ser la unica
cobertura que tenia este camino -- `eval/bargein.py` cuenta como "deteccion" que el
detector de energia baje la voz, que es la SOSPECHA, y usa el STT solo para el lado de
los cortes falsos. La confirmacion, que es la que decide si el paciente puede interrumpir
o no, se apoyaba en una comprobacion que necesita servidor levantado. Este archivo es esa
cobertura.

**Lo que se midio.** Sobre la grabacion exacta que fallaba, con `medium`:

    ventana 0.64 s -> no-voz 0.83, logprob -0.75 -> descartada, texto vacio
    ventana 0.80 s -> no-voz 0.20, logprob -0.68 -> "Normal."

160 ms de audio separan perder la interrupcion de oirla limpia. Y no es que el umbral
este mal: `no_speech_prob` se calcula sobre una ventana de 30 s, asi que con 0.64 s de
audio casi todo es relleno y la cifra no significa nada. Aflojar el umbral habria comprado
esta interrupcion al precio de los cortes falsos, que son el unico numero que este
subsistema mantiene en cero.

**Por que reintentar funciona sin esperar tramas nuevas.** `_resolver_sospecha`
fotografia el audio y luego espera a la transcripcion. El candidato sigue creciendo
durante esos ~250 ms. El fallo no era falta de audio: era usar una foto vieja.

**Y por que hay techo.** Sin el, una sospecha que nunca se confirma deja al detector en
sospecha y al agente con la voz baja para siempre en cuanto el audio deja de llegar --
pestana en segundo plano, red parada. Al agotar los intentos se descarta, que es la
conducta de siempre.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from centinela import main as mod  # noqa: E402

FRECUENCIA = 16000


# ==========================================================================
# Dobles: lo minimo para que `_resolver_sospecha` corra
# ==========================================================================

class DetectorFalso:
    def __init__(self, sospechando: bool = True) -> None:
        self._sospechando = sospechando
        self.descartes = 0
        self.confirmaciones = 0

    @property
    def sospechando(self) -> bool:
        return self._sospechando

    def descartado(self) -> None:
        self.descartes += 1
        self._sospechando = False

    def confirmado(self) -> None:
        self.confirmaciones += 1

    def instantanea(self) -> dict:
        return {}


@dataclass
class CanalFalso:
    """Un canal cuyo candidato crece en cada mirada, como el de verdad.

    `crecimiento_s` es lo que entra de audio mientras el STT trabaja. Con 0.0 se simula
    el caso en que el audio dejo de llegar, que es el que exige el techo de intentos.
    """

    duracion_inicial_s: float = 0.64
    crecimiento_s: float = 0.16
    llamada_id: str = "llamada-de-prueba"
    detector: DetectorFalso = field(default_factory=DetectorFalso)
    miradas: int = 0
    enviados: list = field(default_factory=list)
    olvidos: int = 0

    def audio_candidato(self) -> np.ndarray:
        dur = self.duracion_inicial_s + self.crecimiento_s * self.miradas
        self.miradas += 1
        return np.zeros(int(FRECUENCIA * dur), dtype=np.float32)

    def olvidar_candidato(self) -> None:
        self.olvidos += 1

    async def enviar_json(self, datos: dict) -> None:
        self.enviados.append(datos)


@dataclass
class TransFalsa:
    sin_habla: bool
    texto: str = ""
    motivo_descarte: str | None = None


class SttFalso:
    """Devuelve voz solo cuando la ventana llega a `umbral_s`, como hace `medium`."""

    def __init__(self, umbral_s: float = 0.80) -> None:
        self.umbral_s = umbral_s
        self.ventanas: list[float] = []

    def transcribir(self, audio: np.ndarray) -> TransFalsa:
        dur = len(audio) / FRECUENCIA
        self.ventanas.append(round(dur, 3))
        if dur >= self.umbral_s:
            resultado = TransFalsa(sin_habla=False, texto="Normal.")
        else:
            resultado = TransFalsa(
                sin_habla=True,
                motivo_descarte=f"audio corto: no-voz alta ({dur:.2f} s)",
            )
        return resultado


@pytest.fixture
def cortes(monkeypatch: pytest.MonkeyPatch) -> list:
    """Sustituye el corte real: aqui solo interesa SI se corta, no lo que arrastra."""

    registrados: list = []

    async def falso_cortar(canal, trans):
        registrados.append(trans)

    monkeypatch.setattr(mod, "_cortar_al_agente", falso_cortar)
    return registrados


def _preparar(monkeypatch: pytest.MonkeyPatch, stt: SttFalso) -> None:
    monkeypatch.setitem(mod.E, "stt", stt)
    monkeypatch.setattr(mod.config, "bargein", True, raising=False)


# ==========================================================================
# 1. La ventana corta se reintenta y la interrupcion se confirma
# ==========================================================================

def test_una_ventana_corta_se_vuelve_a_mirar_y_se_confirma(
    monkeypatch: pytest.MonkeyPatch, cortes: list
) -> None:
    """El caso que se rompio: 0.64 s no bastan, 0.80 s si, y nadie manda tramas nuevas."""

    stt = SttFalso(umbral_s=0.80)
    _preparar(monkeypatch, stt)
    canal = CanalFalso(duracion_inicial_s=0.64, crecimiento_s=0.16)

    asyncio.run(mod._resolver_sospecha(canal))

    assert len(cortes) == 1, (
        f"no se confirmo la interrupcion; ventanas miradas: {stt.ventanas}"
    )
    assert stt.ventanas[0] < 0.80, "la primera mirada deberia ser la corta"
    assert stt.ventanas[-1] >= 0.80, "la ultima mirada deberia tener mas audio"
    assert canal.detector.descartes == 0
    assert canal.olvidos == 0, "no se puede tirar el audio del paciente"


def test_si_la_primera_mirada_ya_es_voz_no_se_reintenta(
    monkeypatch: pytest.MonkeyPatch, cortes: list
) -> None:
    """El camino normal no paga ni un milisegundo de mas."""

    stt = SttFalso(umbral_s=0.5)
    _preparar(monkeypatch, stt)

    asyncio.run(mod._resolver_sospecha(CanalFalso(duracion_inicial_s=0.64)))

    assert len(cortes) == 1
    assert len(stt.ventanas) == 1, f"se miro {len(stt.ventanas)} veces, sobraba"


# ==========================================================================
# 2. El techo: la sospecha siempre se cierra
# ==========================================================================

def test_si_el_audio_deja_de_llegar_la_sospecha_se_descarta(
    monkeypatch: pytest.MonkeyPatch, cortes: list
) -> None:
    """El agujero que abriria no acotar el reintento: voz baja para siempre.

    Con `crecimiento_s=0` la ventana nunca mejora. Tiene que acabar descartando y
    devolviendo la voz al agente.
    """

    stt = SttFalso(umbral_s=0.80)
    _preparar(monkeypatch, stt)
    canal = CanalFalso(duracion_inicial_s=0.64, crecimiento_s=0.0)

    asyncio.run(mod._resolver_sospecha(canal))

    assert cortes == []
    assert canal.detector.descartes == 1, "la sospecha se quedo abierta"
    assert canal.olvidos == 1
    assert {"tipo": "subir_voz"} in canal.enviados, "el agente no recupero la voz"
    assert len(stt.ventanas) <= mod.INTENTOS_DE_CONFIRMACION


def test_el_numero_de_intentos_esta_acotado() -> None:
    assert 1 < mod.INTENTOS_DE_CONFIRMACION <= 5
    # Con la espera entre miradas, el retraso anadido en el peor caso.
    peor_ms = mod.MS_ESPERAR_MAS_AUDIO * (mod.INTENTOS_DE_CONFIRMACION - 1)
    assert peor_ms <= 400, f"{peor_ms} ms de espera anadida es demasiado para el barge-in"


# ==========================================================================
# 3. Una ventana ya fiable que dice "no era voz" se acata
#
# Es lo que impide que el reintento degenere en insistir hasta que salga lo que
# queremos: en cuanto hay audio suficiente, un no es un no.
# ==========================================================================

def test_una_ventana_fiable_que_dice_que_no_se_acata(
    monkeypatch: pytest.MonkeyPatch, cortes: list
) -> None:
    stt = SttFalso(umbral_s=99.0)  # nunca dira que es voz
    _preparar(monkeypatch, stt)
    canal = CanalFalso(duracion_inicial_s=mod.MINIMO_FIABLE_PARA_DESCARTAR_S + 0.2)

    asyncio.run(mod._resolver_sospecha(canal))

    assert cortes == []
    assert canal.detector.descartes == 1
    assert len(stt.ventanas) == 1, (
        "una ventana fiable no se reintenta: seria insistir, no comprobar"
    )


def test_el_umbral_de_fiabilidad_deja_sitio_al_caso_medido() -> None:
    """0.64 s tienen que caer dentro del reintento y 0.80 s ser suficientes.

    Son las dos ventanas medidas sobre la grabacion que fallaba. Si alguien baja el
    umbral por debajo de 0.64, el caso vuelve a descartarse en la primera mirada.
    """

    assert mod.MINIMO_FIABLE_PARA_DESCARTAR_S > 0.64
    assert mod.MINIMO_FIABLE_PARA_DESCARTAR_S <= 1.8, (
        "por encima de la ventana que se mira, el reintento no acabaria nunca"
    )
    assert mod.MINIMO_FIABLE_PARA_DESCARTAR_S <= (
        mod.MUESTRAS_A_COMPROBAR / FRECUENCIA
    )
