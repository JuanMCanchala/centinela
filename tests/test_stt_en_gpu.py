"""La GPU no se queda sin usar por una DLL que Windows no encuentra.

Este archivo existe por un fallo que no dio ni un error: el sistema transcribia en CPU
con `small` en una maquina con una RTX 4060 libre, y publicaba las cifras de ese peldano
como si fueran las suyas.

**La cadena.** `ctranslate2.get_cuda_device_count()` devolvia 1. `WhisperModel(...)`
cargaba sin quejarse. La primera inferencia lanzaba
`Library cublas64_12.dll is not found or cannot be loaded`, y la escalera de
configuraciones trata cualquier fallo como "este peldano no sirve" y baja al siguiente.
Tres peldanos despues estaba en `small/cpu/int8`. Todo el mecanismo funcionaba como se
diseno; el diagnostico que faltaba es que la DLL **estaba en el disco**, dentro de los
wheels de NVIDIA, y lo unico que faltaba era que Windows la buscara ahi.

**Lo que costaba** (`eval/escucha.py`, las 18 grabaciones humanas, mismo audio):

    small/cpu/int8              WER 0.126   1 dato clinico mal   1 repregunta
    medium/cuda/int8_float16    WER 0.053   0                    0

Un dato clinico mal de 18 y una repregunta, por una variable de entorno.

**Que se prueba y que no.** Aqui no se prueba que la GPU funcione: eso depende de la
maquina y en la del jurado puede no haber ninguna. Se prueba lo unico que es cierto en
todas: que la funcion busca donde hay que buscar, que no estorba cuando no hay wheels, y
que la escalera la llama **antes** de decidir. Si la GPU esta, `test_la_escalera_elige_gpu_si_esta_disponible`
lo comprueba de verdad; si no, se salta declarandolo.
"""

from __future__ import annotations

import ast
import os
import sys
import sysconfig
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from centinela.stt import whisper as mod  # noqa: E402

RUTA_WHISPER = Path(__file__).resolve().parents[1] / "api" / "centinela" / "stt" / "whisper.py"


@pytest.fixture(autouse=True)
def _path_intacto(monkeypatch: pytest.MonkeyPatch):
    """Cada test parte de un `PATH` propio y del cache de la funcion sin usar."""

    monkeypatch.setenv("PATH", "C:\\nada")
    monkeypatch.setattr(mod, "_dlls_cuda_listas", False)
    yield


# ==========================================================================
# 1. La funcion busca donde estan las DLL
# ==========================================================================

def test_busca_en_el_site_packages_del_interprete(monkeypatch: pytest.MonkeyPatch,
                                                  tmp_path: Path) -> None:
    """Y no en un "Lib/site-packages" escrito a mano, que solo acierta en Windows."""

    for sub in mod.SUBDIRS_CUDA:
        (tmp_path / "nvidia" / sub / "bin").mkdir(parents=True)
    monkeypatch.setattr(sysconfig, "get_paths", lambda: {"purelib": str(tmp_path)})

    anadidos = mod.habilitar_dlls_cuda()

    assert len(anadidos) == len(mod.SUBDIRS_CUDA)
    for sub in mod.SUBDIRS_CUDA:
        assert str(tmp_path / "nvidia" / sub / "bin") in os.environ["PATH"]


def test_las_tres_bibliotecas_que_hacen_falta_estan_declaradas() -> None:
    """`cublas` es la que fallaba; `cudnn` la que falla despues si solo se pone esa."""

    assert "cublas" in mod.SUBDIRS_CUDA
    assert "cudnn" in mod.SUBDIRS_CUDA


def test_el_orden_de_busqueda_pone_las_dll_por_delante(monkeypatch: pytest.MonkeyPatch,
                                                       tmp_path: Path) -> None:
    """Anadir al final dejaria ganar a una cuBLAS vieja instalada en el sistema."""

    (tmp_path / "nvidia" / "cublas" / "bin").mkdir(parents=True)
    monkeypatch.setattr(sysconfig, "get_paths", lambda: {"purelib": str(tmp_path)})
    monkeypatch.setenv("PATH", "C:\\cuda-vieja")

    mod.habilitar_dlls_cuda()

    partes = os.environ["PATH"].split(os.pathsep)
    assert partes[0] == str(tmp_path / "nvidia" / "cublas" / "bin")
    assert "C:\\cuda-vieja" in partes


# ==========================================================================
# 2. No estorba cuando no hay nada que anadir
#
# Es lo que sostiene la compuerta G2: en una maquina sin GPU y sin los wheels, el
# sistema tiene que arrancar exactamente igual que antes de este cambio.
# ==========================================================================

def test_sin_los_wheels_no_toca_el_path(monkeypatch: pytest.MonkeyPatch,
                                        tmp_path: Path) -> None:
    monkeypatch.setattr(sysconfig, "get_paths", lambda: {"purelib": str(tmp_path)})
    antes = os.environ["PATH"]

    anadidos = mod.habilitar_dlls_cuda()

    assert anadidos == []
    assert os.environ["PATH"] == antes


def test_no_falla_si_site_packages_no_existe(monkeypatch: pytest.MonkeyPatch,
                                             tmp_path: Path) -> None:
    monkeypatch.setattr(
        sysconfig, "get_paths", lambda: {"purelib": str(tmp_path / "no-existe")}
    )

    assert mod.habilitar_dlls_cuda() == []


def test_llamarla_dos_veces_no_duplica_el_path(monkeypatch: pytest.MonkeyPatch,
                                               tmp_path: Path) -> None:
    """`_cargar` la llama en cada peldano de la escalera: hasta tres veces por arranque.

    Sin el cerrojo, un `PATH` de Windows crece hasta el limite de la variable y lo que
    se rompe entonces no se parece en nada a su causa.
    """

    (tmp_path / "nvidia" / "cublas" / "bin").mkdir(parents=True)
    monkeypatch.setattr(sysconfig, "get_paths", lambda: {"purelib": str(tmp_path)})

    mod.habilitar_dlls_cuda()
    tras_la_primera = os.environ["PATH"]
    segunda = mod.habilitar_dlls_cuda()

    assert segunda == []
    assert os.environ["PATH"] == tras_la_primera


# ==========================================================================
# 3. La escalera la llama, y la llama antes
#
# Se lee el arbol de sintaxis en vez de espiar la llamada porque lo que importa es el
# ORDEN: `habilitar_dlls_cuda()` despues del `import faster_whisper` seguiria pasando
# un mock de la llamada y volveria a caer a CPU en la maquina de verdad.
# ==========================================================================

def _cuerpo_de(nombre: str) -> list[ast.stmt]:
    arbol = ast.parse(RUTA_WHISPER.read_text(encoding="utf-8"))
    encontrado: list[ast.stmt] = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.FunctionDef) and nodo.name == nombre:
            encontrado = nodo.body
    return encontrado


def test_cargar_habilita_las_dll_antes_de_importar_faster_whisper() -> None:
    cuerpo = _cuerpo_de("_cargar")
    assert cuerpo, "no se encontro _cargar en whisper.py"

    linea_llamada = None
    linea_import = None
    for nodo in ast.walk(ast.Module(body=cuerpo, type_ignores=[])):
        if isinstance(nodo, ast.Call) and getattr(nodo.func, "id", "") == "habilitar_dlls_cuda":
            linea_llamada = nodo.lineno if linea_llamada is None else linea_llamada
        if isinstance(nodo, ast.ImportFrom) and nodo.module == "faster_whisper":
            linea_import = nodo.lineno if linea_import is None else linea_import

    assert linea_llamada is not None, (
        "_cargar no llama a habilitar_dlls_cuda: la escalera volveria a caer a CPU"
    )
    assert linea_import is not None
    assert linea_llamada < linea_import, (
        "habilitar_dlls_cuda se llama despues de importar faster_whisper"
    )


def test_el_primer_peldano_de_la_escalera_es_gpu() -> None:
    """Si alguien reordena la escalera dejando CPU arriba, el arreglo deja de servir."""

    assert mod.ESCALERA[0][1] == "cuda"
    assert mod.ESCALERA[-1][1] == "cpu", "tiene que quedar un peldano sin GPU"


# ==========================================================================
# 4. Con GPU de verdad, si la hay
# ==========================================================================

def _hay_gpu() -> bool:
    try:
        import ctranslate2

        disponible = ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001
        disponible = False
    return disponible


@pytest.mark.skipif(not _hay_gpu(), reason="esta maquina no tiene GPU CUDA")
def test_la_escalera_elige_gpu_si_esta_disponible(monkeypatch: pytest.MonkeyPatch) -> None:
    """La comprobacion que importa: con GPU presente, no se transcribe en CPU.

    Es lenta -- carga el modelo y corre una inferencia real -- y es la unica que habria
    detectado el fallo. Las de arriba comprueban el mecanismo; esta, el efecto.
    """

    for var in ("CENTINELA_STT_MODEL", "CENTINELA_STT_DEVICE", "CENTINELA_STT_COMPUTE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(mod, "_dlls_cuda_listas", False)
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))

    stt = mod.WhisperSTT(
        dir_modelos=Path(__file__).resolve().parents[1] / "data" / "modelos" / "whisper"
    )
    stt.calentar()

    assert stt.validado, f"ningun peldano valido: {stt.intentos}"
    assert stt.dispositivo == "cuda", (
        f"cayo a {stt.tamano}/{stt.dispositivo}: {stt.intentos}"
    )
