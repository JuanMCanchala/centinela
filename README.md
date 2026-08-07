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

### Latencia

Medida con `make bench`. Desglose por etapa, porque un total sin desglose no se puede
auditar.

| Etapa | Medición |
|---|---:|
| Modelo de lenguaje, tiempo hasta el primer token | **185 ms** (p50) |
| Embedding de la consulta (multilingual-e5-large, ONNX) | **51 ms** |
| Transcripción (faster-whisper small int8, CPU) | **16–43 ms** por turno |
| Síntesis de voz, turno de guion (caché pre-renderizado) | **0.001 ms** |
| Síntesis de voz, turno libre (Piper es_MX-ald-medium) | 598 ms · RTF 0.21–0.33 |

El truco de latencia: la conversación la conduce una máquina de estados sobre un guion de
seis dominios, así que **las 35 locuciones que el agente puede decir se conocen antes de
que suene el teléfono**. Se sintetizan una vez y se sirven desde disco. En un turno de
guion —la mayoría de la llamada— el TTS deja de estar en el camino crítico.

### Suite adversarial

`make redteam` · 43/43 casos pasan.

| Grupo | Resultado |
|---|---|
| Intentos de manipulación de instrucciones detectados | 15/15 |
| Peticiones fuera de misión | 6/6 |
| Preguntas clínicas enrutadas al RAG | 6/6 |
| **Falsos positivos sobre habla real del dataset** | **0/8** |

Ese último grupo importa tanto como el primero: un agente que acusa a un paciente
asustado de intentar manipularlo es inservible. Los casos son turnos textuales del
dataset oficial.

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

Centinela clasifica el tema por el texto del documento, no por su ubicación
(`rag/ingest.py`), y la compuerta de fundamentación se niega a responder cuando el corpus
no cubre el procedimiento del paciente. El informe de integridad del corpus
(`docs/informe-corpus.md`) se genera solo y lista todas las incoherencias.

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

Al abrir `http://localhost:8000` hay tres pestañas:

| Pestaña | Qué es |
|---|---|
| **Llamada** | Interfaz de llamada. Micrófono, conversación, y el razonamiento del agente en vivo |
| **Consola de conocimiento** | Subir, listar y borrar documentos, con recibo de olvido |
| **Trazabilidad** | Reglas vigentes con su respaldo documental, y métricas medidas |

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

**`phi3.5:3.8b-mini-instruct-q4_K_M`** vía Ollama, con
**`llama3.2:1b`** evaluado y descartado (ver hallazgo 1).

Ambos están en la lista de modelos permitidos de
[`docs/stack-tecnico.md`](https://github.com/TechSphere2026/ParticipantArtifacts/blob/main/docs/stack-tecnico.md)
del reto. La justificación completa y las mediciones están en
[`docs/informe-final.md`](docs/informe-final.md).

---

## Licencia

MIT. Los PDFs del corpus clínico son obra de sus respectivos autores y se usan solo como
material de referencia del reto. Los datos clínicos del dataset son **sintéticos y no han
sido validados clínicamente**.
