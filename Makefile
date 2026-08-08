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
	@echo "  make muestras    genera el A/B de voz para juzgar la prosodia por oido"
	@echo "  make up          arranca la API + interfaz web en :$(PUERTO)"
	@echo ""
	@echo "  make index       reconstruye el indice del corpus (lento, ~1 h)"
	@echo "  make empacar     comprime data/index/ a data/index.zip (para versionar)"
	@echo "  make extraer     descomprime data/index.zip a data/index/"
	@echo "  make umbrales    resuelve la cita de corpus de cada umbral clinico"
	@echo ""
	@echo "  make eval        motor de decision sobre los 160 casos oficiales"
	@echo "  make humo        extremo a extremo contra la API levantada"
	@echo "  make rag         cobertura del corpus - 0 citas cruzadas, 0 cifras sin respaldo"
	@echo "  make tendencia   barrido de tendencia entre llamadas sobre las 40 trayectorias"
	@echo "  make bargein     interrumpir al agente: latencia, baches y donde se rompe"
	@echo "  make escucha     mide el STT sobre voz humana grabada"
	@echo "  make redteam     suite adversarial (inyeccion, ruido, fuera de mision)"
	@echo "  make bench       latencia de modelo y voz"
	@echo "  make test        todos los tests"
	@echo "  make cifras      las cifras de los documentos contra la medicion real"
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

# Genera el A/B de voz en data/muestras_prosodia/. Existe porque dos cosas de la voz no se
# pueden decidir midiendo -- la velocidad y el modelo -- y la pareja 1_antes / 2_despues
# deja oir el efecto de escribir el guion con tildes.
.PHONY: muestras
muestras:
	$(PY) scripts/muestras_prosodia.py

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

# Tapa el unico hueco clinico del corpus entregado (mastectomia) con guia publica de
# autoridades nombradas, marcada como complementaria para que la cita lo declare.
# El indice ya viene con esto hecho; el objetivo existe para poder reproducirlo.
.PHONY: complementario
complementario:
	$(PY) scripts/ingerir_complementario.py

.PHONY: auditar
auditar:
	$(PY) scripts/auditar_corpus.py

.PHONY: diagrama
diagrama:
	$(PY) scripts/verificar_diagrama.py

# Las cifras que los documentos afirman, contra la medicion real. Existe porque el
# README llego a decir "55 tests" cuando habia 171: no era mentira cuando se escribio,
# se quedo rancia. Con esto no puede quedarse rancia sin que algo falle.
.PHONY: cifras
cifras:
	$(PY) scripts/verificar_cifras.py

# Deja la consola limpia para grabar o demostrar. Respalda antes de borrar y no toca el
# indice del corpus. Sin --aplicar solo enumera. Correr ANTES de medir las metricas de
# la rubrica, no despues: vaciar el estado invalida la muestra que las sostiene.
.PHONY: demo
demo:
	$(PY) scripts/preparar_demo.py

.PHONY: g2
g2:
	powershell -ExecutionPolicy Bypass -File scripts/ensayo_g2.ps1

# ---------------------------------------------------------------- evaluacion

.PHONY: eval
eval:
	$(PY) -m eval.replay_triage

# Mide si el sistema OYE bien, con las grabaciones de eval/audios/.
# `make escucha-guion` imprime que hay que grabar y con que nombre.
.PHONY: escucha
escucha:
	$(PY) -m eval.escucha

.PHONY: escucha-guion
escucha-guion:
	$(PY) -m eval.escucha --guion

# Cuanto del corpus alcanza el agente y con que fidelidad. Falla si aparece una sola
# cita de otro procedimiento o una cifra que el corpus no sostiene.
.PHONY: rag
rag:
	$(PY) -m eval.rag_cobertura

# Barrido de umbrales de tendencia sobre las 40 trayectorias oficiales. La conclusion
# es negativa y por eso esta aca: reproducible en vez de afirmada en un documento.
.PHONY: tendencia
tendencia:
	$(PY) -m eval.tendencia

# Interrumpir al agente. Mezcla las 53 locuciones reales del agente (como eco) con las
# 18 grabaciones de voz humana (como paciente que interrumpe) a atenuaciones de eco
# conocidas. Falla si un solo corte falso sobrevive a las dos capas.
# `--barrido` imprime por que el umbral esta donde esta.
.PHONY: bargein
bargein:
	$(PY) -m eval.bargein

.PHONY: bargein-barrido
bargein-barrido:
	$(PY) -m eval.bargein --barrido --rapido

.PHONY: redteam
redteam:
	$(PY) -m eval.redteam

# Extremo a extremo contra la API levantada. Ya existia como eval/humo.py pero no
# tenia objetivo en el Makefile, asi que el README no la podia citar y la consola
# de pruebas la ejecutaba por un camino que nadie mas usaba.
.PHONY: humo
humo:
	$(PY) -m eval.humo

# Congela las metricas de ejecucion (§5 de la rubrica) desde la API en marcha.
# Correr DESPUES de `make humo` y sobre un servidor recien arrancado; el porque
# esta en scripts/medir_runtime.py.
.PHONY: runtime
runtime:
	$(PY) scripts/medir_runtime.py

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
