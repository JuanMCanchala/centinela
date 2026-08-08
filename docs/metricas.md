# Métricas medidas

> Generado por `scripts/render_metricas.py` a partir de los informes que
> producen los arneses de evaluación. Ninguna cifra se escribe a mano.
> Última generación: 2026-08-08T12:50:29+00:00

## Métricas exigidas por la rúbrica (§5)

Muestra: **25 turnos** en **6 llamadas**, medidos por `obs/metrics.py` durante la ejecución real de la API.

### Latencia de respuesta

> desde que se cierra el VAD (fin de habla del paciente) hasta que el primer byte de audio del agente sale hacia el navegador

| Percentil | Latencia |
|---|---:|
| **P50** | **0.6 ms** |
| **P95** | **390.2 ms** |
| P99 | 4535.6 ms |
| mínimo | 0.4 ms |
| máximo | 5844.6 ms |

El P50 es de milisegundos porque **88 %** de los turnos se responden desde el caché de audio pre-renderizado (22 de 25): la conversación la conduce una máquina de estados, así que las locuciones del guion se conocen antes de que suene el teléfono. El P95 y el P99 son los turnos que sí necesitan sintetizar voz nueva o invocar al modelo.

### Consumo

| Métrica | Valor |
|---|---:|
| Tokens de entrada por turno (P50) | 0.0 |
| Tokens de salida por turno (P50) | 0.0 |
| Tokens de entrada por turno (media) | 111.9 |
| Tokens de salida por turno (media) | 3.3 |
| Tokens de entrada por llamada (media) | **466.3** |
| Tokens de salida por llamada (media) | **13.8** |
| Turnos por llamada (media) | 4.2 |
| Invocaciones al modelo por turno (P50) | **0.0** |
| Invocaciones al modelo por turno (máx) | 1 |
| Consultas al RAG por llamada (media) | **0.17** |
| Consultas al RAG por llamada (máx) | 1 |

Dos cifras se leen mal si no se explican, así que van explicadas.

**El P50 de tokens y de invocaciones al modelo es 0** porque la mayoría de los
turnos no llegan al modelo: si la expresión regular ya extrajo el dolor y el
léxico resolvió el estado de la herida, no hay nada que preguntarle. El modelo
se invoca cuando el turno es ambiguo, y ahí sube a 1. Es la consecuencia de que
la decisión clínica la tome el motor de reglas y no el modelo.

**Las consultas al RAG por llamada (0.17 de media) son bajas** porque el
cuestionario no consulta el corpus: recorre seis dominios con preguntas fijas. El
RAG entra cuando el paciente pregunta algo clínico —*«¿puedo ducharme?»*,
*«¿esto es normal?»*— y entonces la respuesta va fundamentada y con su cita. Una
media alta acá significaría que el agente consulta documentos para preguntar la
temperatura, que sería gasto sin ganancia.

### Costo estimado por llamada

> Centinela corre local; el costo marginal real por llamada es electricidad. Estas cifras son lo que costaria el mismo trafico medido si se sirviera desde APIs comerciales.

| Concepto | USD por llamada |
|---|---:|
| Modelo de lenguaje | 4.8e-05 |
| Transcripción | 0.001036 |
| Síntesis de voz | 0.001512 |
| **Total** | **0.002596** |
| Total en pesos colombianos | $10.4 |

Insumos medidos que entran en el cálculo: tokens entrada por llamada = 466.3 · tokens salida por llamada = 13.8 · turnos por llamada = 4.2 · segundos audio entrada = 33.6 · caracteres tts = 378.0. Las tarifas de referencia están en `obs/metrics.py::PRECIOS_REFERENCIA`.

## Decisión clínica sobre los 160 casos oficiales

Motor `centinela-triage-1.0.0`. Reproducible con `make eval`: el motor no
tiene ninguna fuente de aleatoriedad, así que estas cifras son idénticas en
cada corrida.

| Clase | Recall | Precisión | n real | n predicho |
|---|---:|---:|---:|---:|
| rojo | **1.000** | 1.000 | 12 | 12 |
| amarillo | **1.000** | 0.758 | 25 | 33 |
| verde | **0.935** | 1.000 | 123 | 115 |

| Métrica | Valor |
|---|---:|
| **Falsos negativos clínicos** | **0** |
| Rojos sub-escalados | 0 |
| Exactitud global | 0.950 (152/160) |
| Sobre-escalamientos (dirección segura) | 8 (5.0%) |

Matriz de confusión (filas = etiqueta oficial, columnas = decisión del motor):

| real \ predicho | verde | amarillo | rojo |
|---|---:|---:|---:|
| **verde** | 115 | 8 | 0 |
| **amarillo** | 0 | 25 | 0 |
| **rojo** | 0 | 0 | 12 |

## Suite adversarial

`make redteam` · 42/42 casos (100.0%) en 40.7 s.

| Familia | Pasan |
|---|---:|
| asustado | 2/2 |
| audio degradado | 4/4 |
| degradar hallazgo | 1/1 |
| fuera de mision | 5/5 |
| hostil | 2/2 |
| jerga | 3/3 |
| manipulacion | 10/10 |
| parafraseo rojo | 10/10 |
| pide humano | 2/2 |
| tercero | 3/3 |

- Intentos de manipulación resistidos: **11/11**
- Casos donde la criticidad bajó porque el paciente lo pidió: **0**

## Modelo de lenguaje

`scripts/bench_llm.py`. Se mide el tiempo hasta el primer token porque en una
conversación de voz es lo que el paciente percibe como silencio.

| Modelo | Tarea | TTFT p50 | TTFT máx | tok/s |
|---|---|---:|---:|---:|
| `phi3.5:3.8b-mini-instruct-q4_K_M` | router | **198 ms** | 269 ms | 18.1 |
| `phi3.5:3.8b-mini-instruct-q4_K_M` | extraccion | **191 ms** | 328 ms | 45.5 |
| `phi3.5:3.8b-mini-instruct-q4_K_M` | respuesta_clinica | **199 ms** | 424 ms | 39.4 |
| `llama3.2:1b` | router | **974 ms** | 1032 ms | 10.3 |
| `llama3.2:1b` | extraccion | **961 ms** | 977 ms | 47.2 |
| `llama3.2:1b` | respuesta_clinica | **886 ms** | 944 ms | 36.4 |

El modelo cuatro veces más pequeño resultó ~5× más lento en tiempo hasta el
primer token, así que se descartó la idea de un router barato en 1B y
Phi-3.5 Mini hace los tres trabajos.

## Selección de voz

`scripts/bench_voces.py`. El criterio es el factor de tiempo real (RTF): un
RTF de 1.0 significa que generar un segundo de audio cuesta un segundo.

| Voz | Tamaño | RTF | Elegida |
|---|---:|---:|---|
| `es_ES-carlfm-x_low` | 26.8 MB | 0.334 |  |
| `es_ES-sharvard-medium` | 73.2 MB | 0.345 |  |
| `es_ES-davefx-medium` | 60.3 MB | 0.350 |  |
| `es_MX-ald-medium` | 60.3 MB | 0.352 | **sí** |
| `es_MX-claude-high` | 60.2 MB | 0.403 |  |
| `es_AR-daniela-high` | 108.9 MB | 0.983 |  |

Se eligió `es_MX-ald-medium` y no la de menor RTF: la diferencia entre ambas
es del 5 %, y es la única latinoamericana del grupo rápido. El paciente del
reto es colombiano.

## Camino de voz

| Etapa | Medición |
|---|---:|
| Síntesis desde caché (turno de guion) | **0.001 ms** |
| Síntesis en caliente (19 car) | 104 ms · RTF 0.058 |
| Síntesis en caliente (60 car) | 233 ms · RTF 0.056 |
| Síntesis en caliente (81 car) | 288 ms · RTF 0.058 |
| Respuesta larga completa | 435 ms |
| Primera frase (streaming) | **261 ms** |
| Pre-renderizado del guion completo | 39 locuciones |

### Transcripción

`faster-whisper small` en cpu/int8.

| Duración del audio | Latencia | RTF |
|---|---:|---:|
| 2s | 5 ms | 0.003 |
| 4s | 8 ms | 0.002 |
| 8s | 15 ms | 0.002 |

## Corpus indexado

| Métrica | Valor |
|---|---:|
| Documentos | 106 |
| Páginas | 2084 |
| Fragmentos | 6667 |
| Requirieron OCR | 17 |
| Duplicados lógicos detectados | 2 |
| Incoherencias carpeta/contenido | 18 |

### Cobertura por procedimiento

| Procedimiento | Tema requerido | Documentos | Estado |
|---|---|---:|---|
| Apendicectomia | apendicitis | 23 | cubierto |
| Colecistectomia | colecistitis | 17 | cubierto |
| Colectomia | cancer_colorrectal | 25 | cubierto |
| Mastectomia | cancer_mama | 0 | **SIN COBERTURA** |
| Reemplazo de cadera/rodilla | artroplastia | 22 | cubierto |

---

## Cómo reproducir

```bash
make eval        # decisión clínica sobre los 160 casos
make redteam     # suite adversarial (requiere la API levantada)
make humo        # extremo a extremo (requiere la API levantada)
make bench       # latencia de modelo y voz
make test        # tests unitarios y de regresión
make metricas    # regenera este documento
```

Las métricas exigidas por la rúbrica (§5) se miden sobre la API en marcha,
así que llevan su propia secuencia. El servidor tiene que estar **recién
arrancado**: si se mide sobre uno donde alguien estuvo probando a mano, la
muestra queda llena de llamadas abiertas y abandonadas de dos turnos y las
medias por llamada salen más bajas de lo que corresponde a una llamada real.

```bash
make up                      # servidor limpio, en otra terminal
make humo                    # 6 llamadas completas, ~30 turnos
make runtime                 # congela /api/metricas en docs/metrics/
make metricas                # las escribe acá arriba
```