# Centinela — comandos del proyecto.
#
# El objetivo de diseno de este Makefile es la compuerta G2 del reto: la solucion
# tiene que quedar corriendo en 15 minutos siguiendo solo el README. Por eso
# `make up` es autosuficiente: extrae el indice versionado, baja lo que falte y
# arranca. No hay pasos manuales intermedios.

PY := .venv/Scripts/python.exe
ifeq ($(OS),)
  PY := .venv/bin/python
endif

DATASET ?= ../ParticipantArtifacts/dataset
PUERTO ?= 8000

.PHONY: help
help:
	@echo "Centinela - comandos disponibles"
	@echo ""
	@echo "  make instalar    entorno virtual + dependencias"
	@echo "  make modelos     descarga modelos de voz y embeddings"
	@echo "  make piper       descarga el binario de Piper y las voces en espanol"
	@echo "  make up          arranca la API + interfaz web en :$(PUERTO)"
	@echo ""
	@echo "  make index       reconstruye el indice del corpus (lento, ~1 h)"
	@echo "  make empacar     comprime data/index/ a data/index.zip (para versionar)"
	@echo "  make extraer     descomprime data/index.zip a data/index/"
	@echo "  make umbrales    resuelve la cita de corpus de cada umbral clinico"
	@echo ""
	@echo "  make eval        motor de decision sobre los 160 casos oficiales"
	@echo "  make eval-e2e    pipeline completo sobre las 320 conversaciones"
	@echo "  make redteam     suite adversarial (inyeccion, ruido, fuera de mision)"
	@echo "  make bench       latencia de modelo y voz"
	@echo "  make test        todos los tests"
	@echo "  make metricas    regenera las tablas de metricas del README"

# ---------------------------------------------------------------- instalacion

.PHONY: instalar
instalar:
	uv venv --python 3.11
	uv pip install -r pyproject.toml
	uv pip install pytest==8.3.4

.PHONY: modelos
modelos:
	$(PY) -c "import os,sys; sys.path.insert(0,'api'); os.environ.setdefault('FASTEMBED_CACHE_PATH','data/modelos'); from centinela.rag.embedder import Embedder; e=Embedder(); e.calentar(); print('embeddings listos:', e.nombre_modelo)"
	$(PY) -c "import sys; sys.path.insert(0,'api'); from centinela.stt.whisper import WhisperSTT; from pathlib import Path; s=WhisperSTT(dir_modelos=Path('data/modelos/whisper')); s.calentar(); print('STT listo')"

.PHONY: piper
piper:
	$(PY) scripts/fetch_piper.py

.PHONY: ollama
ollama:
	ollama pull phi3.5:3.8b-mini-instruct-q4_K_M

# ---------------------------------------------------------------- arranque

.PHONY: up
up: extraer
	$(PY) -m uvicorn centinela.main:app --app-dir api --host 0.0.0.0 --port $(PUERTO)

.PHONY: dev
dev: extraer
	$(PY) -m uvicorn centinela.main:app --app-dir api --host 127.0.0.1 --port $(PUERTO) --reload

# ---------------------------------------------------------------- indice

.PHONY: index
index:
	$(PY) scripts/build_index.py --dataset "$(DATASET)/textos" --limpiar

.PHONY: empacar
empacar:
	$(PY) scripts/empacar_index.py empacar

.PHONY: extraer
extraer:
	$(PY) scripts/empacar_index.py extraer

.PHONY: umbrales
umbrales:
	$(PY) scripts/ground_thresholds.py

.PHONY: auditar
auditar:
	$(PY) scripts/auditar_corpus.py

.PHONY: diagrama
diagrama:
	$(PY) scripts/verificar_diagrama.py

.PHONY: g2
g2:
	powershell -ExecutionPolicy Bypass -File scripts/ensayo_g2.ps1

# ---------------------------------------------------------------- evaluacion

.PHONY: eval
eval:
	$(PY) -m eval.replay_triage

.PHONY: eval-e2e
eval-e2e:
	$(PY) -m eval.replay_e2e

.PHONY: redteam
redteam:
	$(PY) -m eval.redteam

# Extremo a extremo contra la API levantada. Ya existia como eval/humo.py pero no
# tenia objetivo en el Makefile, asi que el README no la podia citar y la consola
# de pruebas la ejecutaba por un camino que nadie mas usaba.
.PHONY: humo
humo:
	$(PY) -m eval.humo

.PHONY: bench
bench:
	$(PY) scripts/bench_llm.py
	$(PY) scripts/bench_voz.py

.PHONY: test
test:
	$(PY) -m pytest -q

.PHONY: metricas
metricas: eval redteam
	$(PY) scripts/render_metricas.py
