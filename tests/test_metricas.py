"""Las cifras de §5, que son las que el jurado contrasta contra la sesión.

`obs/metrics.py` no tenía un solo test, y produce exactamente los cuatro números que la
rúbrica exige: latencia P50/P95 hasta el primer audio, tokens por turno **y por llamada**,
invocaciones al modelo por turno y consultas al RAG por llamada. La rúbrica avisa de que
lo reportado se contrasta con los logs, así que un error aquí no es un error de medición:
es una cifra falsa en la entrega.

Esta suite salió de un caso real de ese tipo. El README llegó a decir *"el STT tarda 5.6 s
en P50 sobre este equipo sin GPU"*, y las dos mitades eran falsas: había CUDA, y la
mediana real son 287 ms. El 5.6 s venía de un percentil sobre **n = 2** contaminado por el
arranque en frío. El agregado estaba bien calculado; lo que faltaba era saber sobre cuántas
muestras se calculaba. De ahí el test de que `n` viaje en el resumen.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from centinela.obs.metrics import (  # noqa: E402
    Cronometro,
    MedicionTurno,
    MetricsCollector,
)


def medicion(**kwargs) -> MedicionTurno:
    base = {"llamada_id": "ll1", "turno_idx": 1}
    base.update(kwargs)
    return MedicionTurno(**base)


# ---------------------------------------------------------------- el cronometro

def test_una_etapa_se_mide_y_queda_con_su_nombre() -> None:
    c = Cronometro("ll1", 1)

    with c.etapa("stt"):
        pass

    m = c.cerrar()

    assert "stt" in m.ms_por_etapa
    assert m.ms_por_etapa["stt"] >= 0


def test_la_misma_etapa_dos_veces_en_un_turno_se_suma() -> None:
    """Pasa de verdad: un turno puede sintetizar voz en dos tramos."""

    c = Cronometro("ll1", 1)

    with c.etapa("tts"):
        pass
    primera = c.medicion.ms_por_etapa["tts"]
    with c.etapa("tts"):
        pass

    assert c.medicion.ms_por_etapa["tts"] >= primera


def test_terminar_una_etapa_que_nunca_empezo_no_revienta() -> None:
    c = Cronometro("ll1", 1)

    c.termina("una_que_no_existe")

    assert "una_que_no_existe" not in c.medicion.ms_por_etapa


def test_sin_audio_la_latencia_hasta_el_primer_audio_es_la_del_turno() -> None:
    """Un turno sin voz -- `sin_habla`, o un cierre -- tiene que reportar algo.

    Dejarlo en 0.0 metería un cero en la muestra de la metrica que la rubrica nombra, y
    bajaria el P50 con turnos que nunca produjeron audio.
    """

    c = Cronometro("ll1", 1)

    with c.etapa("stt"):
        pass
    m = c.cerrar()

    assert m.ms_hasta_primer_audio == m.ms_total, (
        "sin marca de primer audio, la metrica de la rubrica cae al total del turno"
    )


def test_marcar_el_primer_audio_congela_la_medida_de_la_rubrica() -> None:
    """La rubrica mide hasta el PRIMER byte, no hasta el final del turno."""

    c = Cronometro("ll1", 1)
    c.primer_audio()
    hasta_el_audio = c.medicion.ms_hasta_primer_audio

    m = c.cerrar()

    assert m.ms_hasta_primer_audio == hasta_el_audio
    assert m.ms_total >= hasta_el_audio


def test_un_turno_tan_rapido_que_mide_cero_sigue_valiendo_cero() -> None:
    """El fallo que destapo una corrida en una maquina cargada, y que no era del test.

    `cerrar()` decidia "nadie marco el primer audio" comprobando si la cifra era 0.0.
    Pero `round(0.004, 2)` ES 0.0: un turno servido del cache lo bastante rapido quedaba
    indistinguible de uno que nunca marco, y el cierre le escribia encima el total del
    turno. En la medida que la rubrica califica, y en el camino que sirve el 83 % de los
    turnos -- donde el P50 medido son 0.6 ms y el minimo 0.4 ms.

    Se fuerza el caso extremo en vez de esperar a que la maquina lo produzca: un test que
    solo falla cuando el equipo va rapido es un test que no protege nada.
    """

    marcado = Cronometro("ll1", 1)
    marcado.medicion.ms_hasta_primer_audio = 0.0
    marcado._audio_marcado = True

    assert marcado.cerrar().ms_hasta_primer_audio == 0.0, (
        "el cierre confundio 'marco cero' con 'no marco' y publico el total del turno"
    )

    # Y el caso complementario sigue funcionando: sin marca, el cierre rellena. Es lo que
    # sostiene la medida de los turnos de texto, que no pasan por `primer_audio()`.
    sin_marcar = Cronometro("ll2", 1)
    with sin_marcar.etapa("extraccion"):
        pass
    cerrado = sin_marcar.cerrar()

    assert cerrado.ms_hasta_primer_audio == cerrado.ms_total


# ---------------------------------------------------------------- el resumen

def test_sin_muestras_lo_dice_en_vez_de_inventar_ceros() -> None:
    r = MetricsCollector()

    assert r.resumen()["n_turnos"] == 0


def test_el_resumen_trae_el_n_de_cada_etapa() -> None:
    """El dato que faltaba cuando el README publico un P50 de STT sobre n=2."""

    r = MetricsCollector()
    r.registrar(medicion(ms_por_etapa={"stt": 300.0}, ms_hasta_primer_audio=300.0))
    r.registrar(medicion(turno_idx=2, ms_por_etapa={"tts": 1.0}, ms_hasta_primer_audio=1.0))
    r.registrar(medicion(turno_idx=3, ms_por_etapa={"tts": 2.0}, ms_hasta_primer_audio=2.0))

    etapas = r.resumen()["por_etapa"]

    assert etapas["stt"]["n"] == 1, "una sola muestra tiene que declararse como una"
    assert etapas["tts"]["n"] == 2


def test_los_percentiles_se_calculan_sobre_las_muestras_que_hay() -> None:
    r = MetricsCollector()
    for i, ms in enumerate([10.0, 20.0, 30.0, 40.0, 1000.0], start=1):
        r.registrar(medicion(turno_idx=i, ms_hasta_primer_audio=ms))

    lat = r.resumen()["latencia_hasta_primer_audio"]

    assert lat["p50_ms"] == 30.0
    assert lat["min_ms"] == 10.0
    assert lat["max_ms"] == 1000.0
    # El P95 interpola entre muestras en vez de devolver el maximo, que con n=5 seria
    # lo mismo que el maximo y no un percentil. Se comprueba que caiga entre las dos
    # muestras que lo rodean, no un valor exacto: eso ataria el test al metodo.
    assert 40.0 < lat["p95_ms"] < 1000.0


def test_tokens_por_llamada_no_es_tokens_por_turno() -> None:
    """La rubrica pide las dos, y son unidades distintas.

    Tres turnos de 100 tokens en una llamada son 100 por turno y 300 por llamada.
    Confundirlas es reportar un tercio de lo que cuesta una llamada.
    """

    r = MetricsCollector()
    for i in range(1, 4):
        r.registrar(medicion(turno_idx=i, tokens_entrada=100, tokens_salida=10))

    res = r.resumen()

    assert res["tokens_por_turno"]["entrada_media"] == 100
    assert res["tokens_por_llamada"]["entrada_media"] == 300
    assert res["tokens_por_llamada"]["turnos_media"] == 3


def test_dos_llamadas_no_se_suman_entre_si() -> None:
    r = MetricsCollector()
    r.registrar(medicion(llamada_id="a", tokens_entrada=100))
    r.registrar(medicion(llamada_id="b", turno_idx=1, tokens_entrada=200))

    res = r.resumen()

    assert res["n_llamadas"] == 2
    assert res["tokens_por_llamada"]["entrada_media"] == 150


def test_se_puede_resumir_una_sola_llamada() -> None:
    r = MetricsCollector()
    r.registrar(medicion(llamada_id="a", tokens_entrada=100))
    r.registrar(medicion(llamada_id="b", turno_idx=1, tokens_entrada=999))

    res = r.resumen(llamada_id="a")

    assert res["n_turnos"] == 1
    assert res["tokens_por_turno"]["entrada_media"] == 100


def test_la_proporcion_desde_cache_cuenta_turnos_y_no_bytes() -> None:
    """Es la cifra con la que se explica un P50 de milisegundos."""

    r = MetricsCollector()
    r.registrar(medicion(tts_desde_cache=True))
    r.registrar(medicion(turno_idx=2, tts_desde_cache=True))
    r.registrar(medicion(turno_idx=3, tts_desde_cache=False))

    tts = r.resumen()["tts"]

    assert tts["turnos_servidos_desde_cache"] == 2
    assert round(tts["proporcion_desde_cache"], 3) == 0.667


# ---------------------------------------------------------------- persistencia

def test_cada_turno_deja_una_linea_en_el_jsonl(tmp_path) -> None:
    """Es lo que permite contrastar lo reportado contra los logs de la sesion."""

    ruta = tmp_path / "metricas.jsonl"
    r = MetricsCollector(ruta=ruta)

    r.registrar(medicion(ms_hasta_primer_audio=12.5, invocaciones_llm=1))
    r.registrar(medicion(turno_idx=2, ms_hasta_primer_audio=0.4))

    lineas = [json.loads(l) for l in ruta.read_text(encoding="utf-8").splitlines() if l.strip()]

    assert len(lineas) == 2
    assert lineas[0]["ms_hasta_primer_audio"] == 12.5
    assert lineas[0]["invocaciones_llm"] == 1
    assert lineas[1]["turno_idx"] == 2


def test_la_memoria_esta_acotada_pero_el_archivo_no(tmp_path) -> None:
    """Un servidor de dias no puede crecer sin limite en RAM.

    El archivo si conserva todo: es la evidencia, y `medir_runtime.py` lee de ahi.
    """

    ruta = tmp_path / "metricas.jsonl"
    r = MetricsCollector(ruta=ruta, maximo_en_memoria=3)

    for i in range(1, 8):
        r.registrar(medicion(turno_idx=i))

    assert r.resumen()["n_turnos"] == 3
    assert len(ruta.read_text(encoding="utf-8").strip().splitlines()) == 7
