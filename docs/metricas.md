# Métricas medidas

> Generado por `scripts/render_metricas.py` a partir de los informes que
> producen los arneses de evaluación. Ninguna cifra se escribe a mano.
> Última generación: 2026-08-09T18:04:01+00:00

## Métricas exigidas por la rúbrica (§5)

Muestra: **82 turnos** en **20 llamadas**, medidos por `obs/metrics.py` durante la ejecución real de la API.

### Latencia de respuesta

> desde que se cierra el VAD (fin de habla del paciente) hasta que el primer byte de audio del agente sale hacia el navegador

| Percentil | Latencia |
|---|---:|
| **P50** | **0.8 ms** |
| **P95** | **2738.2 ms** |
| P99 | 5892.6 ms |
| mínimo | 0.4 ms |
| máximo | 6338.0 ms |

El P50 es de milisegundos porque **83 %** de los turnos se responden desde el caché de audio pre-renderizado (68 de 82): la conversación la conduce una máquina de estados, así que las locuciones del guion se conocen antes de que suene el teléfono. El P95 y el P99 son los turnos que sí necesitan sintetizar voz nueva o invocar al modelo.

#### Partido por camino

Sobre los **7579 turnos** medidos en `data/runtime/metricas.jsonl`, no sobre la ventana en memoria de un proceso.

| Camino | n | P50 | P95 | P99 |
|---|---:|---:|---:|---:|
| todos | 7579 | 0.6 ms | 2154.0 ms | 5837.7 ms |
| voz del agente desde cache | 6850 | 0.6 ms | 125.0 ms | 2338.9 ms |
| voz del agente sintetizada en el turno | 729 | 465.0 ms | 6604.8 ms | 15389.0 ms |
| con invocacion al modelo | 473 | 2554.5 ms | 11024.6 ms | 16341.4 ms |
| con consulta al corpus | 108 | 6002.4 ms | 16235.3 ms | 17376.3 ms |
| turno de voz (entro por el microfono) | 278 | 434.9 ms | 13232.5 ms | 16649.5 ms |

El histórico cruza un cambio de sistema, así que el camino de voz se publica separado por configuración de transcripción: mezclarlas daría un percentil que no describe ninguna de las dos.

| Configuración | n | P50 | P95 |
|---|---:|---:|---:|
| voz con STT medium/cuda | 46 | 177.0 ms | 3117.0 ms |
| voz con STT sin registrar | 232 | 461.1 ms | 13732.4 ms |

Vigente: **medium/cuda**.

#### Desde que el paciente deja de hablar

> desde que el paciente deja de hablar hasta el primer byte de audio del agente, endpointing incluido. Es la definicion con la que se publican los benchmarks de agentes de voz.

| Medida | Valor |
|---|---:|
| P50 con cierre adaptativo (450 ms) | **627.0 ms** |
| P50 con el techo (900 ms) | 1077.0 ms |
| P95 con el techo | 4017.0 ms |

> el turno cierra a los 450 ms cuando la respuesta ya resuelve el dominio preguntado, y espera el techo de 900 ms cuando no. `eval/escucha.py` mide cuantas grabaciones cierran pronto. Medido sobre voz con STT medium/cuda (46 turnos).

### Consumo

| Métrica | Valor |
|---|---:|
| Tokens de entrada por turno (P50) | 0.0 |
| Tokens de salida por turno (P50) | 0.0 |
| Tokens de entrada por turno (media) | 113.0 |
| Tokens de salida por turno (media) | 7.0 |
| Tokens de entrada por llamada (media) | **463.1** |
| Tokens de salida por llamada (media) | **28.9** |
| Turnos por llamada (media) | 4.1 |
| Invocaciones al modelo por turno (P50) | **0.0** |
| Invocaciones al modelo por turno (máx) | 1 |
| Consultas al RAG por llamada (media) | **0.2** |
| Consultas al RAG por llamada (máx) | 1 |

Dos cifras se leen mal si no se explican, así que van explicadas.

**El P50 de tokens y de invocaciones al modelo es 0** porque la mayoría de los
turnos no llegan al modelo: si la expresión regular ya extrajo el dolor y el
léxico resolvió el estado de la herida, no hay nada que preguntarle. El modelo
se invoca cuando el turno es ambiguo, y ahí sube a 1. Es la consecuencia de que
la decisión clínica la tome el motor de reglas y no el modelo.

**Las consultas al RAG por llamada (0.2 de media) son bajas** porque el
cuestionario no consulta el corpus: recorre seis dominios con preguntas fijas. El
RAG entra cuando el paciente pregunta algo clínico —*«¿puedo ducharme?»*,
*«¿esto es normal?»*— y entonces la respuesta va fundamentada y con su cita. Una
media alta acá significaría que el agente consulta documentos para preguntar la
temperatura, que sería gasto sin ganancia.

### Costo estimado por llamada

> Centinela corre local; el costo marginal real por llamada es electricidad. Estas cifras son lo que costaria el mismo trafico medido si se sirviera desde APIs comerciales.

| Concepto | USD por llamada |
|---|---:|
| Modelo de lenguaje | 4.9e-05 |
| Transcripción | 0.001011 |
| Síntesis de voz | 0.001476 |
| **Total** | **0.002537** |
| Total en pesos colombianos | $10.1 |

Insumos medidos que entran en el cálculo: tokens entrada por llamada = 463.1 · tokens salida por llamada = 28.9 · turnos por llamada = 4.1 · segundos audio entrada = 32.8 · caracteres tts = 369.0. Las tarifas de referencia están en `obs/metrics.py::PRECIOS_REFERENCIA`.

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

`make redteam` · 43/43 casos (100.0%) en 40.7 s.

| Familia | Pasan |
|---|---:|
| asustado | 2/2 |
| audio degradado | 4/4 |
| degradar hallazgo | 2/2 |
| fuera de mision | 5/5 |
| hostil | 2/2 |
| jerga | 3/3 |
| manipulacion | 10/10 |
| parafraseo rojo | 10/10 |
| pide humano | 2/2 |
| tercero | 3/3 |

- Intentos de manipulación resistidos: **12/12**
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
| Síntesis desde caché (turno de guion) | **1.599 ms** |
| Síntesis en caliente (19 car) | 106 ms · RTF 0.067 |
| Síntesis en caliente (60 car) | 239 ms · RTF 0.062 |
| Síntesis en caliente (81 car) | 272 ms · RTF 0.056 |
| Respuesta larga completa | 744 ms |
| Primera frase (streaming) | **296 ms** |
| Pre-renderizado del guion completo | 45 locuciones |

### Transcripción

`faster-whisper medium` en cuda/int8_float16, medido sobre 18 grabaciones de voz humana (`eval/audios`, ficheros fijos).

| Métrica | Valor |
|---|---:|
| Latencia mediana | **231 ms** |
| RTF mediano | 0.155 |
| RTF peor caso | 0.258 |
| Arranque del modelo | 3.3 s |

> latencia sobre voz humana real. La version anterior media audio sintetico (np.zeros + ruido 0.001) con vad_filter activo: el VAD lo descartaba entero, no se decodificaba nada, y de ahi salian los 5 ms por 2 s de audio (RTF 0.003) que se publicaron. Sobre voz de verdad la misma configuracion tardaba ~1100 ms. La exactitud no se mide aqui: esta en scripts/bench_stt.py y eval/escucha.py.

## Corpus indexado

| Métrica | Valor |
|---|---:|
| Documentos | 107 |
| Páginas | 2097 |
| Fragmentos | 6690 |
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