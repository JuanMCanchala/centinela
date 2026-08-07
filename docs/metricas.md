# Métricas medidas

> Generado por `scripts/render_metricas.py` a partir de los informes que
> producen los arneses de evaluación. Ninguna cifra se escribe a mano.
> Última generación: 2026-08-07T23:18:09+00:00

## Métricas exigidas por la rúbrica (§5)

Muestra: **21 turnos** en **4 llamadas**, medidos por `obs/metrics.py` durante la ejecución real de la API.

### Latencia de respuesta

> desde que se cierra el VAD (fin de habla del paciente) hasta que el primer byte de audio del agente sale hacia el navegador

| Percentil | Latencia |
|---|---:|
| **P50** | **0.6 ms** |
| **P95** | **385.3 ms** |
| P99 | 5062.9 ms |
| mínimo | 0.4 ms |
| máximo | 6232.4 ms |

El P50 es de milisegundos porque **90 %** de los turnos se responden desde el caché de audio pre-renderizado (19 de 21): la conversación la conduce una máquina de estados, así que las locuciones del guion se conocen antes de que suene el teléfono. El P95 y el P99 son los turnos que sí necesitan sintetizar voz nueva o invocar al modelo.

### Consumo

| Métrica | Valor |
|---|---:|
| Tokens de entrada por turno (P50) | 0.0 |
| Tokens de salida por turno (P50) | 0.0 |
| Tokens de entrada por turno (media) | 133.2 |
| Tokens de salida por turno (media) | 4.1 |
| Tokens de entrada por llamada (media) | **699.5** |
| Tokens de salida por llamada (media) | **21.8** |
| Turnos por llamada (media) | 5.2 |
| Invocaciones al modelo por turno (P50) | **0.0** |
| Invocaciones al modelo por turno (máx) | 1 |
| Consultas al RAG por llamada (media) | **0.25** |
| Consultas al RAG por llamada (máx) | 1 |

Dos cifras se leen mal si no se explican, así que van explicadas.

**El P50 de tokens y de invocaciones al modelo es 0** porque la mayoría de los
turnos no llegan al modelo: si la expresión regular ya extrajo el dolor y el
léxico resolvió el estado de la herida, no hay nada que preguntarle. El modelo
se invoca cuando el turno es ambiguo, y ahí sube a 1. Es la consecuencia de que
la decisión clínica la tome el motor de reglas y no el modelo.

**Las consultas al RAG por llamada (0.25 de media) son bajas** porque el
cuestionario no consulta el corpus: recorre seis dominios con preguntas fijas. El
RAG entra cuando el paciente pregunta algo clínico —*«¿puedo ducharme?»*,
*«¿esto es normal?»*— y entonces la respuesta va fundamentada y con su cita. Una
media alta acá significaría que el agente consulta documentos para preguntar la
temperatura, que sería gasto sin ganancia.

### Costo estimado por llamada

> Centinela corre local; el costo marginal real por llamada es electricidad. Estas cifras son lo que costaria el mismo trafico medido si se sirviera desde APIs comerciales.

| Concepto | USD por llamada |
|---|---:|
| Modelo de lenguaje | 7.2e-05 |
| Transcripción | 0.001283 |
| Síntesis de voz | 0.001872 |
| **Total** | **0.003227** |
| Total en pesos colombianos | $12.9 |

Insumos medidos que entran en el cálculo: tokens entrada por llamada = 699.5 · tokens salida por llamada = 21.8 · turnos por llamada = 5.2 · segundos audio entrada = 41.6 · caracteres tts = 468.0. Las tarifas de referencia están en `obs/metrics.py::PRECIOS_REFERENCIA`.

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

`make redteam` · 32/32 casos (100.0%) en 23.5 s.

| Familia | Pasan |
|---|---:|
| asustado | 2/2 |
| audio degradado | 4/4 |
| degradar hallazgo | 1/1 |
| fuera de mision | 5/5 |
| hostil | 2/2 |
| jerga | 3/3 |
| manipulacion | 10/10 |
| pide humano | 2/2 |
| tercero | 3/3 |

- Intentos de manipulación resistidos: **11/11**
- Casos donde la criticidad bajó porque el paciente lo pidió: **0**

## Modelo de lenguaje

`scripts/bench_llm.py`. Se mide el tiempo hasta el primer token porque en una
conversación de voz es lo que el paciente percibe como silencio.

| Modelo | Tarea | TTFT p50 | TTFT máx | tok/s |
|---|---|---:|---:|---:|
| `phi3.5:3.8b-mini-instruct-q4_K_M` | router | **185 ms** | 198 ms | 20.6 |
| `phi3.5:3.8b-mini-instruct-q4_K_M` | extraccion | **170 ms** | 252 ms | 66.1 |
| `phi3.5:3.8b-mini-instruct-q4_K_M` | respuesta_clinica | **184 ms** | 291 ms | 59.9 |
| `llama3.2:1b` | router | **879 ms** | 981 ms | 11.3 |
| `llama3.2:1b` | extraccion | **986 ms** | 1021 ms | 46.1 |
| `llama3.2:1b` | respuesta_clinica | **914 ms** | 1007 ms | 36.7 |

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
| Síntesis en caliente (19 car) | 598 ms · RTF 0.332 |
| Síntesis en caliente (60 car) | 904 ms · RTF 0.213 |
| Síntesis en caliente (81 car) | 1307 ms · RTF 0.256 |
| Respuesta larga completa | 1545 ms |
| Primera frase (streaming) | **958 ms** |
| Pre-renderizado del guion completo | 35 locuciones |

### Transcripción

`faster-whisper small` en cpu/int8.

| Duración del audio | Latencia | RTF |
|---|---:|---:|
| 2s | 16 ms | 0.008 |
| 4s | 24 ms | 0.006 |
| 8s | 43 ms | 0.005 |

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