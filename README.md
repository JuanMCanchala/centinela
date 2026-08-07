# Centinela

**Agente de voz para seguimiento postoperatorio.** Llama al paciente, conversa en español
colombiano, reconstruye su cuadro clínico hablando, y decide si hay que alertar a un
humano. Cada respuesta clínica cita el documento que la sustenta; cada decisión cita la
regla que la disparó.

Entrega para el **Tech Sphere Challenge 2026** (Source Meridian).

> **Estado:** en construcción. Este README se actualiza con cada avance. Las cifras de
> las tablas de métricas las generan los scripts de `eval/` y `scripts/`, no se escriben
> a mano.

---

## La idea en una frase

> **El modelo percibe. El código decide.**

Un modelo de 3.8 B de parámetros no es de fiar para decidir si alguien se está
complicando después de una cirugía. Pero sí es bueno convirtiendo *"me duele como aquí
abajito de la axila hace como 20 minutos"* en datos tipados. Así que el agente está
partido en dos mitades con responsabilidades separadas:

```
voz → percepción (modelo, difusa) → estado clínico tipado → decisión (código, determinista) → acción
```

- **Percepción** (`clinical/extractor.py`): el modelo extrae seis campos clínicos con
  esquema JSON forzado. Nunca decide nada.
- **Decisión** (`clinical/triage_engine.py`): reglas deterministas versionadas, con cada
  umbral respaldado por una cita al corpus. Auditable, reproducible, y **inmune a
  inyección de prompt**: no existe frase capaz de convencer a `fiebre >= 38.0` de ser falso.
- **Conducción** (`dialog/policy.py`): el flujo de la conversación es una máquina de
  estados. El modelo no elige la siguiente pregunta, así que no se le puede hablar para
  que se salga del protocolo.

---

## Resultados medidos

### Decisión clínica sobre los 160 casos oficiales del reto

Reproducible con `make eval`. El motor no tiene ninguna fuente de aleatoriedad, así que
estos números son los mismos en cada corrida.

| Clase | Recall | Precisión | n real |
|---|---:|---:|---:|
| rojo | **1.000** | **1.000** | 12 |
| amarillo | **1.000** | 0.758 | 25 |
| verde | 0.935 | 1.000 | 123 |

- **Falsos negativos clínicos: 0.** Ningún caso rojo o amarillo cerrado en verde.
- Exactitud global: 152/160 = **0.950**
- Los 8 errores son verdes sobre-escalados a amarillo — la dirección segura según el
  principio de asimetría clínica de la rúbrica.

### Las cuatro métricas que exige la rúbrica (§5)

Medidas por `obs/metrics.py` durante la ejecución real, congeladas con `make runtime` y
escritas por `make metricas`. **Ninguna se escribe a mano**, porque la rúbrica advierte que
lo reportado se contrasta con los logs de la sesión. Muestra: **21 turnos en 4 llamadas
completas**, la que produce `make humo` sobre un servidor recién arrancado.

**1. Latencia de respuesta** — *desde que se cierra el VAD (fin de habla del paciente)
hasta que el primer byte de audio del agente sale hacia el navegador.*

| Percentil | Latencia |
|---|---:|
| **P50** | **0.6 ms** |
| **P95** | **385.3 ms** |
| P99 | 5 062.9 ms |

El P50 es de milisegundos porque **90 % de los turnos se sirven desde el caché de audio
pre-renderizado** (19 de 21): la conversación la conduce una máquina de estados, así que
las locuciones del guion se conocen antes de que suene el teléfono. El P95 son los turnos
que sintetizan voz nueva. El P99 es un solo turno —el que invoca al modelo *y* consulta el
RAG— y con 21 muestras un P99 es un dato anecdótico, no una estadística; se reporta por
completitud, no como promesa.

**2. Consumo por turno y por llamada**

| Métrica | Valor |
|---|---:|
| Tokens de entrada / salida por turno (P50) | **0 / 0** |
| Tokens de entrada / salida por turno (media) | 133.2 / 4.1 |
| Tokens de entrada / salida **por llamada** (media) | **699.5 / 21.8** |
| Turnos por llamada (media) | 5.2 |

**3. Invocaciones al modelo por turno** — **P50 = 0**, máximo 1. La mayoría de los turnos
no llegan al modelo: si la regex ya extrajo el dolor y el léxico resolvió la herida, no hay
nada que preguntarle. Es la consecuencia directa de que la decisión clínica la tome el
motor de reglas.

**4. Consultas al RAG por llamada** — **0.25 de media**, máximo 1. Son bajas a propósito:
el cuestionario no consulta el corpus, recorre seis dominios con preguntas fijas. El RAG
entra cuando el paciente pregunta algo clínico —*«¿puedo ducharme?»*— y entonces la
respuesta va fundamentada y con su cita. Una media alta aquí significaría que el agente
consulta documentos para preguntar la temperatura: gasto sin ganancia.

**Costo estimado por llamada: USD 0.003227** (COP 12.9). Centinela corre local, así que el
costo marginal real es electricidad; la rúbrica pide extrapolar a precios de API y explicar
el cálculo. Son los tokens y segundos de audio realmente medidos por tarifas públicas de
referencia: modelo USD 0.000072 · transcripción USD 0.001283 · voz USD 0.001872. Las
tarifas están en `obs/metrics.py::PRECIOS_REFERENCIA` y los insumos en
[`docs/metricas.md`](docs/metricas.md), que se genera del mismo `runtime.json` que estas
cifras — si divergen, es que alguien editó una a mano.

Para reproducirlas hace falta un servidor **recién arrancado** — si se mide sobre uno donde
alguien estuvo probando a mano, la muestra se llena de llamadas abandonadas de dos turnos:

```bash
make up          # en otra terminal
make humo        # 6 llamadas completas
make runtime     # congela /api/metricas
make metricas    # las escribe en docs/metricas.md
```

### Latencia por etapa

Medida con `make bench`. Desglose por etapa, porque un total sin desglose no se puede
auditar.

| Etapa | Medición |
|---|---:|
| Modelo de lenguaje, tiempo hasta el primer token | **185 ms** (p50) |
| Embedding de la consulta (multilingual-e5-large, ONNX) | **51 ms** |
| Transcripción (faster-whisper `medium`, CUDA int8_float16) | **252–709 ms** por turno |
| Síntesis de voz, turno de guion (caché pre-renderizado) | **0.001 ms** |
| Síntesis de voz, turno libre (Piper es_MX-ald-medium) | 598 ms · RTF 0.21–0.33 |
| **Turno completo resuelto por reglas** | **< 1 ms** |
| **Turno completo que necesita el modelo** | ~2.2 s |

El STT arranca por una escalera con validación real: `medium` en CUDA, y si la GPU no
responde baja a `small` en CUDA y luego a CPU. Lo que se cargó de hecho lo dice
`/api/salud` en `stt.dispositivo`, y la escalera está en `stt/whisper.py::ESCALERA`.

Dos decisiones sostienen el presupuesto:

**El guion va en caché.** La conversación la conduce una máquina de estados sobre seis
dominios, así que las **53 locuciones** que el agente puede decir se conocen antes de que
suene el teléfono. Se sintetizan al arrancar y se sirven desde disco; la proporción de
turnos servidos así es la que aparece arriba, medida, no estimada.

**El modelo solo cuando hace falta.** Si la regex extrajo el dolor y el léxico resolvió la
herida, no se invoca — de ahí que el P50 de invocaciones sea 0. La optimización quedó
registrada en `eval/probar_tokens.py`, sobre un escenario fijo de seis turnos: **1359 → 672
tokens de entrada por llamada**. Es un escenario fijo, distinto de la muestra de §5, así que
su cifra no tiene por qué coincidir con los 699.5 tokens/llamada de arriba: son dos
mediciones de dos cosas distintas y las dos son reales.

### Suite adversarial

`make redteam` corre 32 casos adversariales **contra el sistema completo**, no contra el
clasificador aislado. Resultado: **32/32**.

| Familia | Pasan |
|---|---:|
| Manipulación de instrucciones | 10/10 |
| Fuera de misión | 5/5 |
| Audio degradado | 4/4 |
| Tercero que interrumpe | 3/3 |
| Jerga regional colombiana | 3/3 |
| Paciente hostil / asustado | 4/4 |
| Pide hablar con un humano | 2/2 |
| Intento de retirar un hallazgo ya detectado | 1/1 |

- Intentos de manipulación resistidos: **11/11**
- Casos donde la criticidad bajó porque el paciente lo pidió: **0**

Más `make test`: **55 tests**, que incluyen cero falsos positivos de manipulación sobre
turnos textuales del dataset oficial. Ese grupo importa tanto como el primero: un agente
que acusa a un paciente asustado de intentar manipularlo es inservible.

### Compuerta de arranque (G2)

`scripts/ensayo_g2.ps1` clona el repo en un directorio limpio y cronometra los pasos del
README, uno por uno.

| Paso | Tiempo |
|---|---:|
| Clonar el repositorio | 3.6 s |
| `make instalar` | 3.3 s |
| `make piper` (con descargas en caché) | 0.9 s |
| `make modelos` (con descargas en caché) | 2.9 s |
| Extraer el índice del corpus | 0.6 s |
| `make up` hasta la primera respuesta | 8.2 s |
| Verificación funcional (llamada + caso rojo + consola) | 1.1 s |
| **Total medido** | **21 s** |
| **Peor caso estimado en máquina virgen** (+4.6 GB de descargas) | **~5 min** |

El límite del reto son 15 minutos. El margen en el peor caso es de **10 minutos**.

---

## Tres hallazgos sobre el material entregado

### 1. La mitad de los modelos permitidos ya no existe

La compuerta G3 del reto descalifica por usar un modelo fuera de la lista. La lista tiene
cuatro opciones y a agosto de 2026 dos están apagadas:

| Modelo permitido | Estado verificado | Usable |
|---|---|---|
| Google Gemini 1.5 Flash | Toda la familia Gemini 1.5 devuelve 404 | No |
| Llama 3.1 70B (Groq) | `llama-3.1-70b-versatile` decomisionado | No |
| Llama 3.2 (1B / 3B) local | Vivo, vía Ollama | Sí |
| Phi-3.5 Mini (3.8B) local | Vivo, vía Ollama | Sí |

Así que el reto es forzosamente local. Entre las dos opciones vivas **medimos en vez de
suponer** (`scripts/bench_llm.py`):

| Modelo | TTFT p50 | Throughput | Formato en extracción |
|---|---:|---:|---|
| **Phi-3.5 Mini 3.8B** | **185 ms** | 66 tok/s | JSON correcto |
| Llama 3.2 1B | 880 ms | 46 tok/s | Verboso, se sale del formato |

El modelo cuatro veces más pequeño resultó cinco veces más lento en lo único que el
paciente percibe. Se descartó la idea del "router barato en 1B" y **Phi-3.5 Mini hace los
tres trabajos**.

### 2. El corpus tiene un hueco clínico de 8 pacientes

`dataset/textos/breast_cancer/` contiene 19 PDFs. **Ninguno es de cáncer de mama: todos
son de cáncer de cuello uterino.** Mientras tanto, `perfiles_clinicos` tiene 8 pacientes
(20 % de la población) con procedimiento **Mastectomía**.

Un RAG que enrute por nombre de carpeta le sirve guías de cérvix a una paciente
mastectomizada con total confianza. Eso no es un error de recuperación: es la
**alucinación clínica peligrosa** que la rúbrica penaliza y anota textualmente en el acta.

No es hipotético: nos pasó. Antes de añadir el filtro por tema, la pregunta *"¿cuándo
puedo volver a hacer ejercicio?"* para una paciente de **colecistectomía** devolvía como
mejor pasaje una guía de cáncer de cuello uterino, y el modelo respondía citándola con
total seguridad. Lo encontró `eval/humo.py`.

Centinela clasifica el tema por el texto del documento y no por su ubicación
(`rag/ingest.py`), filtra la recuperación por el procedimiento del paciente
(`rag/retriever.py`), y la compuerta de fundamentación se niega a responder cuando el
corpus no lo cubre. La auditoría del corpus (`make auditar`, resultado en
`docs/informe-corpus.md`) lo reporta:

| Defecto detectado en el material entregado | Cantidad |
|---|---:|
| Documentos cuyo contenido no corresponde a su carpeta | 18 |
| Duplicados lógicos (mismo artículo, distinto nombre y distintos bytes) | 2 |
| Documentos sin capa de texto, resueltos con OCR | 17 |
| Procedimientos de pacientes sin ningún documento que los cubra | 1 |

Los duplicados no los detecta ningún hash de archivo: son el mismo artículo con las
ligaduras codificadas distinto. Se atrapan comparando términos distintivos del texto.

### 3. El agente de referencia del dataset nunca escala

En `caso_tray_pac_42_00026_7` (etiquetado **rojo**), la paciente reporta líquido amarillo
saliendo de la herida quirúrgica. El agente del dataset responde:

> *"Gracias por esos detalles. Cambiando de tema, ¿cómo ha estado su apetito?"*

Recoge el dato y sigue el guion. Centinela hace lo contrario: **rompe el cuestionario en
el turno donde aparece la bandera roja**, entrega la instrucción de seguridad citada, dice
qué va a pasar, y dispara el escalamiento (`dialog/policy.py`,
`_interrumpir_por_bandera_roja`).

---

## Arranque

### Requisitos

- Python 3.11+ y [uv](https://docs.astral.sh/uv/)
- [Ollama](https://ollama.com/) corriendo en `localhost:11434`
- 8 GB de RAM. GPU opcional (acelera, no hace falta)

### Puesta en marcha

```bash
git clone <este-repo> centinela && cd centinela

make instalar        # entorno virtual + dependencias
make ollama          # descarga Phi-3.5 Mini (2.4 GB)
make piper           # binario de TTS + voces en español
make modelos         # embeddings + transcripción
make up              # arranca en http://localhost:8000
```

El índice del corpus viene **ya construido** en `data/index.zip` y `make up` lo extrae
solo. Reconstruirlo desde los 107 PDFs toma cerca de una hora por el OCR, así que no se
reconstruye en el arranque.

Al abrir `http://localhost:8000` hay una consola de operaciones con cuatro pestañas y,
a la izquierda, la **cola de llamadas** ordenada por criticidad — rojo primero, porque
la cola es una lista de trabajo y el trabajo urgente va antes. Al pulsar una llamada ya
cerrada se abre en modo lectura, con su hoja de traspaso y su transcripción.

| Pestaña | Qué es |
|---|---|
| **Llamada** | Micrófono, conversación, y el razonamiento del agente en vivo. Arriba, *la tira* |
| **Conocimiento** | Subir, listar y borrar documentos, con recibo de olvido |
| **Motor y métricas** | Reglas vigentes con su respaldo documental, y métricas medidas |
| **Pruebas** | Corre las suites de este README desde el navegador y muestra su salida |

**La tira** es el gráfico de arriba de la vista de llamada, tomado de la hoja de
anestesia: el tiempo corre de izquierda a derecha, cada dominio clínico tiene su carril
y se rellena en el turno en que se supo el dato, y el nivel se dibuja como una escalera
que **solo puede subir**. Eso último no es una decisión estética sino una propiedad del
motor con un test que la prueba (`test_manipulacion_no_baja_una_criticidad_ya_establecida`):
establecida una bandera, ningún texto posterior la retira.

La pestaña **Pruebas** ejecuta cada suite como un subproceso y el veredicto es su código
de salida — el mismo comando que aparece en [Verificar la entrega](#verificar-la-entrega),
sin una segunda implementación dentro del panel. Si el número que muestra la consola no
coincide con el del informe, es que el informe está desactualizado.

La interfaz no tiene paso de compilación: es HTML, CSS y JS servidos tal cual. Las
tipografías (Chivo y Chivo Mono, SIL OFL) van **versionadas en el repo** en
`web/fuentes/`, así que la consola no necesita red; `python scripts/fetch_fuentes.py`
las vuelve a bajar si hiciera falta.

### Reconstruir el índice desde cero

```bash
make index DATASET=../ParticipantArtifacts/dataset
make empacar
```

---

## Estructura

```
api/centinela/
├── main.py                 API: consola, llamada, WebSocket de voz, métricas
├── models.py               tipos del dominio clínico (contrato percepción→decisión)
├── config.py               configuración por entorno
├── clinical/
│   ├── thresholds.py       umbrales, con la consulta que resuelve su cita al corpus
│   ├── triage_engine.py    MOTOR DE DECISIÓN — sin modelo de lenguaje
│   ├── extractor.py        habla → estado clínico tipado (3 capas)
│   └── normalizer.py       español colombiano: ruido, registro, números por regla
├── dialog/
│   ├── script.py           guion clínico canónico (fuente del caché de audio)
│   ├── policy.py           máquina de estados que conduce la llamada
│   └── guardrails.py       intención del turno y detección de manipulación
├── rag/
│   ├── ingest.py           PDF → texto → chunks, con OCR y dedup por contenido
│   ├── store.py            Chroma + SQLite, con tombstones y recibo de olvido
│   ├── embedder.py         embeddings ONNX, sin PyTorch
│   ├── retriever.py        híbrido BM25 + denso, RRF, MMR, compuerta de fundamentación
│   └── answerer.py         respuesta clínica + verificación posterior a la generación
├── escalation/service.py   ticket persistente + resumen con forma FHIR
├── stt/whisper.py          faster-whisper
├── tts/piper.py            Piper con proceso residente y caché pre-renderizado
└── obs/metrics.py          instrumentación por etapa
eval/                       arneses de evaluación reproducibles
scripts/                    índice, benchmarks, empaquetado
prompts/                    prompts versionados, uno por archivo
web/                        frontend sin paso de compilación
```

---

## Modelo declarado (compuerta G3)

**`phi3.5:3.8b-mini-instruct-q4_K_M`** vía Ollama, con **`llama3.2:1b`** evaluado y
descartado (ver hallazgo 1).

Ambos están en la lista de modelos permitidos de
[`docs/stack-tecnico.md`](https://github.com/TechSphere2026/ParticipantArtifacts/blob/main/docs/stack-tecnico.md)
del reto. Se verifica en `api/centinela/config.py`, en el `Makefile` y en `GET /api/salud`.

---

## Documentación

| Documento | Qué contiene |
|---|---|
| [`docs/informe-final.md`](docs/informe-final.md) | Informe final: declaración del modelo, decisión técnica, riesgos, proceso |
| [`docs/arquitectura.md`](docs/arquitectura.md) | Diagramas de arquitectura y flujo de decisión, verificados contra el código |
| [`docs/metricas.md`](docs/metricas.md) | Todas las métricas medidas, generadas por script |
| [`docs/informe-corpus.md`](docs/informe-corpus.md) | Auditoría de integridad del corpus entregado |
| [`prompts/`](prompts/) | Prompts versionados, uno por archivo, con su esquema y notas |

## Verificar la entrega

Las cuatro primeras también se pueden lanzar desde la pestaña **Pruebas** del panel, que
ejecuta exactamente estos comandos.

```bash
make test        # 161 tests unitarios y de regresión
make eval        # 160 casos oficiales · cero falsos negativos clínicos
make redteam     # 32 casos adversariales (requiere la API levantada)
make humo        # 67 comprobaciones de extremo a extremo (requiere la API levantada)
make diagrama    # cada elemento del diagrama existe en el código
make auditar     # defectos detectados en el corpus entregado
make bench       # latencia de modelo y voz
make g2          # ensayo cronometrado de la compuerta de arranque
make metricas    # regenera docs/metricas.md desde las mediciones
```

---

## Licencia

MIT. Los PDFs del corpus clínico son obra de sus respectivos autores y se usan solo como
material de referencia del reto. Los datos clínicos del dataset son **sintéticos y no han
sido validados clínicamente**.
