"""La latencia que se publica describe algo, y describe el sistema de hoy.

Tres defectos de la medición, no del sistema. Los tres hacían que la cifra publicada
fuera cierta y a la vez no significara nada.

**Uno: la muestra.** Se publicaba el P95 de la ventana en memoria del proceso vivo --
42 turnos -- mientras `data/runtime/metricas.jsonl` tenía miles medidos en disco. Un P99
sobre 42 muestras es el máximo con decimales.

**Dos: la mezcla.** Esos turnos son dos poblaciones que difieren en cuatro órdenes de
magnitud: una locución del guion sale del caché en 0.6 ms y un turno que consulta el
corpus tarda segundos. Una sola cifra sobre la mezcla se mueve con la proporción de
preguntas del guion que tenga la muestra, no con el sistema.

**Tres: la línea de salida.** `ms_hasta_primer_audio` arranca cuando el VAD declara fin
de habla, así que deja fuera el endpointing. La rúbrica nombra ese instante y la medición
es correcta, pero los benchmarks de agentes de voz miden desde que el llamante calla.
Comparar los dos números es comparar contra otra línea de salida, unos cientos de
milisegundos más adelante.

Y un cuarto que apareció al arreglar los otros: el histórico **cruza un cambio de
sistema**. Mover el STT de `small/cpu` a `medium/cuda` bajó el P50 del camino de voz de
461 ms a 172 ms, y el P95 de 13 732 a 2 198. Un percentil sobre las dos poblaciones
juntas describe un sistema que no existe. De ahí el campo `stt` en cada medición: sin él,
la única forma de separarlas es una fecha escrita a mano en un script.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))
sys.path.insert(0, str(RAIZ / "scripts"))

from centinela.obs.metrics import Cronometro, MedicionTurno  # noqa: E402

import medir_runtime as mr  # noqa: E402


# ==========================================================================
# 1. Cada medición dice con qué se midió
# ==========================================================================

def test_la_medicion_tiene_campo_de_configuracion_de_stt() -> None:
    assert "stt" in MedicionTurno(llamada_id="x", turno_idx=0).a_dict()


def test_el_campo_viaja_al_json() -> None:
    m = MedicionTurno(llamada_id="x", turno_idx=0)
    m.stt = "medium/cuda"
    assert json.loads(json.dumps(m.a_dict()))["stt"] == "medium/cuda"


def test_sin_configuracion_es_none_y_no_cadena_vacia() -> None:
    """`None` significa "no se sabe"; "" se confundiria con una configuracion real."""

    assert MedicionTurno(llamada_id="x", turno_idx=0).stt is None


def test_el_servidor_rellena_la_configuracion() -> None:
    """Guarda estructural: que `_empaquetar_turno` siga poniendo el campo.

    Sin esto el campo existe, sale `None` siempre, y todo el historico futuro vuelve a
    ser inseparable -- callando, que es lo peor.
    """

    fuente = (RAIZ / "api" / "centinela" / "main.py").read_text(encoding="utf-8")
    assert "medicion.stt = " in fuente, (
        "_empaquetar_turno ya no anota la configuracion de STT"
    )


# ==========================================================================
# 2. La latencia se publica partida, y sobre el histórico
# ==========================================================================

@pytest.fixture
def historico(tmp_path: Path) -> Path:
    """Un histórico de juguete con las dos poblaciones que hacían falta separar."""

    filas = []
    # Preguntas del guion desde cache: rapidisimas.
    for i in range(50):
        filas.append({"llamada_id": "a", "turno_idx": i, "ms_hasta_primer_audio": 0.5,
                      "tts_desde_cache": True, "invocaciones_llm": 0, "consultas_rag": 0,
                      "ms_por_etapa": {}, "stt": "medium/cuda"})
    # Turnos de voz con el sistema viejo: lentos.
    for i in range(30):
        filas.append({"llamada_id": "b", "turno_idx": i, "ms_hasta_primer_audio": 9000.0,
                      "tts_desde_cache": False, "invocaciones_llm": 1, "consultas_rag": 1,
                      "ms_por_etapa": {"stt": 8000.0}, "stt": "small/cpu"})
    # Y con el nuevo: rapidos.
    for i in range(25):
        filas.append({"llamada_id": "c", "turno_idx": i, "ms_hasta_primer_audio": 200.0,
                      "tts_desde_cache": False, "invocaciones_llm": 0, "consultas_rag": 0,
                      "ms_por_etapa": {"stt": 130.0}, "stt": "medium/cuda"})
    ruta = tmp_path / "metricas.jsonl"
    ruta.write_text(
        "\n".join(json.dumps(f, ensure_ascii=False) for f in filas) + "\n",
        encoding="utf-8",
    )
    return ruta


def test_se_lee_el_historico_entero_y_no_una_ventana(historico: Path) -> None:
    r = mr.por_camino(historico)
    assert r["disponible"]
    assert r["n_turnos_medidos"] == 105


def test_los_caminos_se_publican_separados(historico: Path) -> None:
    r = mr.por_camino(historico)
    por_nombre = {c["camino"]: c for c in r["caminos"]}

    assert por_nombre["voz del agente desde cache"]["n"] == 50
    assert por_nombre["voz del agente desde cache"]["p50_ms"] < 10
    assert por_nombre["con consulta al corpus"]["n"] == 30
    assert por_nombre["con consulta al corpus"]["p50_ms"] > 1000
    # Y la mezcla, que es la cifra que se publicaba sola, queda entre las dos.
    assert por_nombre["todos"]["n"] == 105


def test_el_camino_de_voz_se_parte_por_configuracion(historico: Path) -> None:
    """La comprobación que importa: los 9000 ms del sistema viejo no contaminan al nuevo."""

    r = mr.por_camino(historico)
    por_cfg = {c["camino"]: c for c in r["voz_por_configuracion"]}

    assert set(por_cfg) == {"voz con STT small/cpu", "voz con STT medium/cuda"}
    assert por_cfg["voz con STT medium/cuda"]["p95_ms"] < 1000, (
        "el percentil de la configuracion nueva arrastra turnos de la vieja"
    )
    assert por_cfg["voz con STT small/cpu"]["p95_ms"] > 1000


def test_la_configuracion_vigente_es_la_del_final(historico: Path) -> None:
    assert mr.por_camino(historico)["configuracion_vigente"] == "medium/cuda"


def test_los_turnos_sin_campo_se_publican_aparte_y_no_se_suman(tmp_path: Path) -> None:
    """No se sabe con qué se midieron. Eso es un dato, no un estorbo."""

    ruta = tmp_path / "viejo.jsonl"
    ruta.write_text(json.dumps({
        "llamada_id": "a", "turno_idx": 0, "ms_hasta_primer_audio": 5000.0,
        "ms_por_etapa": {"stt": 4000.0},
    }) + "\n", encoding="utf-8")

    r = mr.por_camino(ruta)
    nombres = [c["camino"] for c in r["voz_por_configuracion"]]
    assert nombres == ["voz con STT sin registrar"]


# ==========================================================================
# 3. El endpointing va dentro del número comparable
# ==========================================================================

def test_el_extremo_a_extremo_suma_el_endpointing(historico: Path) -> None:
    caminos = mr.por_camino(historico)
    e2e = mr.extremo_a_extremo(caminos)

    assert e2e["disponible"]
    voz_vigente = next(
        c for c in caminos["voz_por_configuracion"]
        if c["camino"] == "voz con STT medium/cuda"
    )
    assert e2e["p50_ms_con_cierre_adaptativo"] == pytest.approx(
        voz_vigente["p50_ms"] + mr.MS_ENDPOINTING_ADAPTATIVO
    )
    assert e2e["p50_ms_con_techo"] > e2e["p50_ms_con_cierre_adaptativo"]


def test_el_extremo_a_extremo_se_mide_sobre_la_configuracion_vigente(
    historico: Path,
) -> None:
    e2e = mr.extremo_a_extremo(mr.por_camino(historico))
    assert e2e["medido_sobre"] == "voz con STT medium/cuda"


def test_el_endpointing_declarado_es_el_del_cliente() -> None:
    """Si alguien cambia el VAD del navegador, esta cifra deja de ser cierta.

    Se lee de `web/app.js` en vez de confiar en la constante, porque el número que el
    paciente sufre lo decide el cliente.
    """

    app = (RAIZ / "web" / "app.js").read_text(encoding="utf-8")
    assert f"msSilencioParaCerrar: {mr.MS_ENDPOINTING_TECHO:.0f}" in app, (
        "el techo de silencio del cliente ya no es el que declara medir_runtime"
    )


# ==========================================================================
# 4. El guardián de muestra cuenta turnos de VOZ
#
# El de antes contaba turnos totales, y los de texto son mayoría: aprobaba con 42
# turnos cuando el camino de voz tenía 13 muestras.
# ==========================================================================

def test_hay_un_minimo_propio_para_la_muestra_de_voz() -> None:
    assert mr.MINIMO_TURNOS_DE_VOZ >= 20


def test_pocos_turnos_de_voz_se_declaran_insuficientes(tmp_path: Path) -> None:
    filas = [{"llamada_id": "a", "turno_idx": i, "ms_hasta_primer_audio": 1.0,
              "tts_desde_cache": True, "ms_por_etapa": {}, "stt": "medium/cuda"}
             for i in range(500)]
    filas += [{"llamada_id": "b", "turno_idx": i, "ms_hasta_primer_audio": 300.0,
               "ms_por_etapa": {"stt": 200.0}, "stt": "medium/cuda"}
              for i in range(3)]
    ruta = tmp_path / "m.jsonl"
    ruta.write_text("\n".join(json.dumps(f) for f in filas) + "\n", encoding="utf-8")

    r = mr.por_camino(ruta)

    assert r["n_turnos_medidos"] == 503
    assert not r["muestra_de_voz_suficiente"], (
        "500 turnos de texto no autorizan a publicar el P95 del camino de voz"
    )


def test_un_historico_que_no_existe_no_rompe_la_medicion(tmp_path: Path) -> None:
    r = mr.por_camino(tmp_path / "no-existe.jsonl")
    assert r == {"disponible": False}
    assert mr.extremo_a_extremo(r) == {"disponible": False}


# ==========================================================================
# 5. El cronómetro sigue midiendo lo que la rúbrica nombra
# ==========================================================================

def test_el_cronometro_mide_hasta_el_primer_audio() -> None:
    crono = Cronometro("x", 0)
    with crono.etapa("stt"):
        pass
    crono.primer_audio()
    m = crono.cerrar()

    assert m.ms_hasta_primer_audio > 0
    assert "stt" in m.ms_por_etapa
