# Centinela

**Agente de voz para seguimiento postoperatorio.** Llama al paciente, conversa en español
colombiano, reconstruye su cuadro clínico hablando, y decide si hay que alertar a un
humano. Cada respuesta clínica cita el documento que la sustenta; cada decisión cita la
regla que la disparó.

Entrega para el **Tech Sphere Challenge 2026** (Source Meridian).

## Entregables

| | |
|---|---|
| **Video demo** | **https://youtu.be/o6HEGjZ64vo** |
| **Informe final** | [`docs/informe-final.md`](docs/informe-final.md) |
| **Diagrama** | [`docs/arquitectura.md`](docs/arquitectura.md) |
| **Repositorio** | este |

**Stack de modelos y voz**, declarado de entrada:

| | | |
|---|---|---|
| Razonamiento | **`phi3.5:3.8b-mini-instruct-q4_K_M`** vía Ollama | local · [compuerta G3](#modelo-declarado-compuerta-g3) |
| Transcripción (STT) | **faster-whisper `medium`** (CTranslate2, con Silero VAD) | local |
| Síntesis de voz (TTS) | **Piper** `es_ES-davefx-medium` | local |
| Voz del agente | **Chatterbox Multilingual** (Resemble AI, MIT) — clonación de una grabación con consentimiento, **pre-renderizada** a `data/audio_clon/` | fuera de ejecución |
| Embeddings | **multilingual-e5-large** vía fastembed (ONNX, sin PyTorch) | local |
| Base vectorial | **ChromaDB** + BM25 (recuperación híbrida) | local |

**No hace falta ninguna clave de API**: los seis corren en local. Ver [`.env.example`](.env.example)
para las variables de entorno, todas opcionales.

> Todas las cifras de este documento las generan los scripts de `eval/` y `scripts/`.
> Ninguna se escribe a mano: la rúbrica advierte que lo reportado se contrasta con los
> logs de la sesión.

---

## La idea en una frase

> **El modelo percibe. El código decide.**

Una decisión clínica tiene tres propiedades que no son negociables. Es **reproducible**:
el mismo cuadro da el mismo nivel siempre, hoy y en un mes. Es **auditable**: se explica
por la regla exacta que la disparó y por el dato que la cumplió. Es **citable**: cada
umbral apunta a un documento con página y frase.

Ningún modelo generativo da las tres, y no es una cuestión de tamaño — muestrear una
distribución no es decidir. Así que en Centinela **la decisión clínica es código**:
comparaciones numéricas versionadas, cada una con su cita al corpus.

Lo que hace el modelo es aquello en lo que ninguna regla lo iguala: convertir *"me duele
como aquí abajito de la axila hace como 20 minutos"* en datos tipados.

```
voz → percepción (modelo, difusa) → estado clínico tipado → decisión (código, determinista) → acción
```

- **Percepción** (`clinical/extractor.py`): tres capas —regex, léxico y modelo— producen
  seis campos clínicos con esquema JSON forzado. Ninguna decide nada, y un hallazgo de
  alarma detectado por regla **nunca se degrada** aunque el modelo diga lo contrario.
- **Decisión** (`clinical/triage_engine.py`): reglas deterministas versionadas. Aquí no
  hay ni una llamada al modelo, y eso compra tres propiedades comprobables: no puede
  alucinar una decisión porque no hay nada que muestrear; es **inmune a inyección de
  prompt**, porque no existe frase capaz de convencer a `fiebre >= 38.0` de ser falso; y
  el replay de los 160 casos oficiales da el mismo resultado en cada corrida.
- **Conducción** (`dialog/policy.py`): el flujo de la conversación es una máquina de
  estados. El modelo no elige la siguiente pregunta, así que no se le puede hablar para
  que se salga del protocolo.

---

## Garantías operativas

Lo que el sistema garantiza cuando algo va mal, con el mecanismo que lo sostiene y el
comando que lo comprueba. El detalle de operación está en
[`docs/operacion.md`](docs/operacion.md).

| Garantía | Mecanismo | Se comprueba con |
|---|---|---|
| Una bandera roja produce alerta **en el turno**, no al cerrar | `EscalationService.escalar_ahora` | `make humo` caso 9 |
| Ninguna llamada queda abierta: si cuelgan, si el cliente desaparece o si el proceso se reinicia, se cierra y produce su resumen | handler de WS + barredor cada 30 s + recuperación al arrancar | `make humo` caso 10 · `make test` |
| Cerrar sin haber preguntado todo **nunca** da verde | `triage_engine`, asimetría clínica | `make test` |
| La alerta sale del proceso, con reintentos y sin duplicar | outbox durable + despachador | `make humo` caso 9 |
| Un rojo sin acuse en 15 min se reporta como fuera de plazo | `alertas_vencidas` + pestaña Alertas | `GET /api/alertas` |
| Ninguna respuesta clínica cita otro procedimiento ni usa una cifra que el corpus no sostenga | filtro por tema + verificación posterior a la generación | `make rag` |
| El paciente puede cortarle la palabra al agente, y una tos no se lo corta | umbral sobre el eco medido + confirmación con el STT | `make bargein` |
| Una pregunta que el paciente no llegó a oír es una pregunta **no hecha**: vuelve al guion sin gastar un intento | `DialogPolicy.marcar_interrumpido` | `make humo` caso 11 |
| **La instrucción de urgencias se oye entera**: la llamada no cuelga antes, y si la interrumpen se repite | `PAPEL_URGENTE` + `_retomar_urgencia`; el registro dice si se oyó | `make humo` caso 2 · `make test` |
| Antes de mandar a alguien a urgencias, el agente **lee de vuelta** lo que entendió | `dialog/confirmacion.py`; la alerta **no** espera la confirmación | `make humo` caso 2 |
| Ni un desmentido ni una corrección a la baja retiran una alerta ya creada | `_con_piso_de_criticidad` + las dos versiones en el registro | `make redteam` familia `degradar_hallazgo` |
| Un bache de red no cuesta la llamada: el canal se cae, vuelve y la llamada sigue con su estado clínico | ventana de gracia en el servidor + reconexión con espera creciente | `make humo` casos 10 y 12 |

Una llamada que se abre y en la que el paciente nunca habla **no** produce alerta
clínica: queda la constancia del intento de contacto. A alguien con quien no se habló no
se le puede hacer triaje, y una bandeja con ruido es una bandeja que nadie lee.

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
lo reportado se contrasta con los logs de la sesión. Muestra: **300 turnos en 73 llamadas
completas**, de las cuales **90 turnos entran por voz de verdad** —`make humo` sobre un
servidor recién arrancado, más las llamadas completas de `eval/conversacion_voz.py` con las
grabaciones de voz humana de `eval/audios/` y las de `eval/silencio.py`.

Los turnos de audio son los que hacen honesta la medición: el jurado habla por micrófono,
así que una muestra sin ellos mide una conversación que nadie tiene.

**1. Latencia de respuesta** — *desde que se cierra el VAD (fin de habla del paciente)
hasta que el primer byte de audio del agente sale hacia el navegador.*

Se publica **partida por camino**, sobre los 8 695 turnos medidos en
`data/runtime/metricas.jsonl`. Una sola cifra sobre la mezcla no describe nada: un turno
del guion sale del caché en 0.6 ms y uno que consulta el corpus tarda segundos, así que
el número se movería con la proporción de preguntas del guion que tuviera la muestra, no
con el sistema. La tabla completa la genera `make metricas` en
[`docs/metricas.md`](docs/metricas.md); aquí van las dos filas que importan:

| Camino | n | P50 | P95 |
|---|---:|---:|---:|
| Pregunta del guion, voz desde caché | 7 835 | **0.6 ms** | 151.0 ms |
| Turno de voz, configuración vigente (`medium/cuda`) | 152 | **185.3 ms** | 2 765.6 ms |

El P50 del primer camino es de microsegundos porque **85 % de los turnos se sirven desde
el caché de audio pre-renderizado**: la conversación la conduce una máquina de estados,
así que las locuciones del guion se conocen antes de que suene el teléfono.

**Y contada como la cuenta el mercado**, que mide desde que el llamante deja de hablar
—endpointing incluido— y no desde que el VAD cierra:

| Medida | Valor |
|---|---:|
| P50 con cierre adaptativo (450 ms) | **635.3 ms** |
| P50 con el techo del cliente (900 ms) | 1 085.3 ms |
| P95 con el techo | 3 665.6 ms |

Los 635 ms de mediana quedan dentro del umbral en que una conversación se siente natural.
El P95 no: son los turnos donde el modelo tiene que intervenir, y es el trabajo que sigue
abierto.

**Estas cifras costaron tres correcciones, y las tres merecen contarse.**

La primera fue de medición. El README decía *"el STT tarda 5.6 s en P50 sobre este equipo
sin GPU"*, y las dos mitades eran falsas: hay CUDA (`medium` en `cuda/int8_float16`,
validada con inferencia real) y la mediana son decenas de milisegundos. Aquel 5.6 s venía
de una muestra de **n = 2** cuyas dos únicas transcripciones eran arranques en frío. La
muestra ahora trae 13 turnos con audio real.

La segunda fue un defecto de verdad, y lo destapó justamente medir bien. Con la muestra
arreglada, el P95 del STT seguía en **9 909 ms**, y los tres picos eran todos turnos
posteriores a una interrupción. Contando invocaciones se vio por qué: **una llamada con
7.7 s de audio real transcribía 172.6 s en 55 invocaciones** —22 veces el audio que
existía—. Cada veredicto del detector de barge-in lanzaba su propia comprobación y perdía
la referencia a la anterior, que seguía transcribiendo el candidato —y el candidato crece
mientras el paciente habla, así que el trabajo total crecía con el cuadrado del número de
veredictos. El turno no era lento: **competía por la GPU con veinte veces más trabajo del
necesario**.

Con una comprobación en vuelo a la vez, y mirando una ventana en vez del candidato
completo, la misma llamada hace **3 invocaciones y transcribe 9.4 s**, y el turno tras la
interrupción pasa de 7 852 ms a **531 ms**.

El intercambio, dicho: los baches a −12 dB pasan de 0.00 a **0.23 por minuto** —uno cada
cuatro minutos— porque mientras una comprobación está en vuelo el detector no puede lanzar
otra y la voz se queda baja un poco más. Un bache baja el volumen 250 ms y el agente
sigue; lo que nunca pasa es perder el turno, y eso sigue en **0 cortes falsos con 100 % de
detección hasta −12 dB** (`make bargein`, tres corridas).

La tercera es la más incómoda, porque **la afirmación de arriba se volvió falsa sin que
nada avisara**. Este README decía que había CUDA y que el STT corría en
`medium/cuda/int8_float16` —y era cierto cuando se escribió—. Después dejó de serlo: la
escalera de configuraciones probaba `medium/cuda`, la primera inferencia fallaba con
`Library cublas64_12.dll is not found or cannot be loaded`, y bajaba dos peldaños hasta
`small/cpu`. Siguió atendiendo llamadas, más lento y con más error, sin una línea de log
que lo llamara problema, mientras el documento publicaba las cifras del peldaño de arriba.
La DLL estaba en el disco, en los wheels de NVIDIA: en Windows Python no la busca ahí.

Lo que costaba, medido sobre las 18 grabaciones humanas de `eval/audios`:

| | Latencia | WER medio | Dato clínico mal | Repreguntas |
|---|---:|---:|---:|---:|
| `small/cpu` (lo que corría) | 1 054 ms | 0.126 | **1** | **1 de 18** |
| `medium/cuda` (lo que decía) | **231 ms** | **0.053** | 0 | 0 |

De ahí que `GET /api/salud` publique ahora `degradado_a_cpu`, que cada medición de
`metricas.jsonl` anote con qué configuración se tomó, y que el P95 del camino de voz se
publique separado por configuración: mezclar las dos poblaciones daba 13 732 ms, un
percentil que no describe ninguno de los dos sistemas.

**2. Consumo por turno y por llamada**

| Métrica | Valor |
|---|---:|
| Tokens de entrada / salida por turno (P50) | **0 / 0** |
| Tokens de entrada / salida por turno (media) | 54.4 / 9.1 |
| Tokens de entrada / salida **por llamada** (media) | **223.5 / 37.5** |
| Turnos por llamada (media) | 4.1 |

Estas cuatro se miden sobre la ventana del proceso vivo, así que se mueven con lo que se
haya ejecutado antes. Las regenera `make runtime && make metricas`; las de la tabla salen
de la última corrida sobre `make humo` más cuatro llamadas por voz.

**3. Invocaciones al modelo por turno** — **P50 = 0**, media 0.09, máximo 1. La mayoría de
los turnos no llegan al modelo: si la regex ya extrajo el dolor y el léxico resolvió la
herida, no hay nada que preguntarle. Es la consecuencia directa de que la decisión clínica
la tome el motor de reglas.

Y hay un turno que **nunca** llega al modelo, por construcción: el de confirmar identidad.
El agente acaba de preguntar *«¿es usted X?»*, así que no hay nada clínico que extraer.
Costaba **2 385 ms y 301 tokens** para que el modelo respondiera que no había datos, y es
justo el turno con el que se verifica la compuerta G4 del reto —*«saludo y una pregunta
trivial»*—: era el más lento del sistema y el primero que oye quien evalúa. Ahora cuesta
**6 ms**. Las dos capas de reglas siguen corriendo, así que un paciente que se adelanta
—*«sí soy yo, y estoy con treinta y ocho y medio de fiebre»*— entra igual, con su bandera
roja, en 0.4 ms.

**4. Consultas al RAG por llamada** — **0.05 de media**, máximo 2. Son bajas a propósito:
el cuestionario no consulta el corpus, recorre seis dominios con preguntas fijas. El RAG
entra cuando el paciente pregunta algo clínico —*«¿puedo ducharme?»*— y entonces la
respuesta va fundamentada y con su cita. Una media alta aquí significaría que el agente
consulta documentos para preguntar la temperatura: gasto sin ganancia.

**Costo estimado por llamada: USD 0.002513** (COP 10.1). Centinela corre local, así que el
costo marginal real es electricidad; la rúbrica pide extrapolar a precios de API y explicar
el cálculo. Son los tokens y segundos de audio realmente medidos por tarifas públicas de
referencia: modelo USD 0.000026 · transcripción USD 0.001011 · voz USD 0.001476. Las
tarifas están en `obs/metrics.py::PRECIOS_REFERENCIA` y los insumos en
[`docs/metricas.md`](docs/metricas.md), que se genera del mismo `runtime.json` que estas
cifras — si divergen, es que alguien editó una a mano.

Para reproducirlas hace falta un servidor **recién arrancado** — si se mide sobre uno donde
alguien estuvo probando a mano, la muestra se llena de llamadas abandonadas de dos turnos:

```bash
make up                # en otra terminal
make humo              # llamadas completas de extremo a extremo
make runtime           # congela /api/metricas
make metricas          # las escribe en docs/metricas.md
make cifras-corregir   # y las escribe en este README, encima de las viejas
```

El último paso existe porque **las cifras de esta sección se escribían a mano y se
quedaron rancias todas a la vez**: la muestra decía 42 turnos cuando eran 82, el costo por
llamada decía USD 0.002425 cuando era 0.002537, el P95 del camino de voz decía 2 197.7 ms
cuando era 3 117.0, y una frase remitía a «los 415.4 tokens/llamada de arriba» cuando
arriba ponía 462.2. Ninguna era mentira cuando se escribió. El verificador declaraba por
escrito que las de latencia y consumo quedaban fuera «porque ya vienen de un archivo
generado» — y era cierto que ese archivo se genera, y falso que el README leyera de él.
Ahora `make cifras` compara **45 cifras** contra `docs/metrics/runtime.json` y
`cifras-corregir` las escribe; las cuatro que se cuentan del código —el número de tests—
siguen a mano a propósito, porque ahí lo que cambió es el sistema.

### Latencia por etapa

Medida con `make bench`. Desglose por etapa, porque un total sin desglose no se puede
auditar. Son p50 de cinco repeticiones en la misma máquina, con el servidor y Ollama
corriendo al lado: es la condición real de la demo, y por eso una corrida puede variar un
10 % de la siguiente. La síntesis de voz escala con la longitud del texto —de ahí el
rango— y el RTF se mantiene plano, que es lo que importa.

| Etapa | Medición |
|---|---:|
| Modelo de lenguaje, tiempo hasta el primer token | **198 ms** (p50) |
| Embedding de la consulta (multilingual-e5-large, ONNX) | **51 ms** |
| Transcripción (faster-whisper `medium`, CUDA int8_float16) | **252–709 ms** por turno |
| Síntesis de voz, turno de guion (caché pre-renderizado) | **0.001 ms** |
| Síntesis de voz, turno libre (Piper es_MX-ald-medium) | 104–466 ms · RTF 0.056–0.058 |
| **Turno completo resuelto por reglas** | **< 1 ms** |
| **Turno completo que necesita el modelo** | ~2.2 s |

El STT arranca por una escalera con validación real: `medium` en CUDA, y si la GPU no
responde baja a `small` en CUDA y luego a CPU. Lo que se cargó de hecho lo dice
`/api/salud` en `stt.dispositivo`, y la escalera está en `stt/whisper.py::ESCALERA`.

Dos decisiones sostienen el presupuesto:

**El guion va en caché.** La conversación la conduce una máquina de estados sobre seis
dominios, así que las **61 locuciones** que el agente puede decir se conocen antes de que
suene el teléfono. Se sintetizan al arrancar y se sirven desde disco; la proporción de
turnos servidos así es la que aparece arriba, medida, no estimada.

El caché se invalida por **contenido**, no por nombre de archivo: `data/audio_cache/manifiesto.json`
guarda de qué texto y con qué tratamiento salió cada WAV. Antes la clave nombraba el
archivo, así que editar una locución no cambiaba lo que el paciente oía — el WAV viejo
seguía en disco y se servía igual.

### Cómo habla el agente

Cuatro cosas medibles separan «una voz sintética» de «alguien al teléfono», y ninguna es el
timbre del modelo.

**1. El fonemizador lee la ortografía.** Piper fonemiza con espeak-ng, que deduce la sílaba
tónica de cómo está escrita la palabra. El guion estuvo escrito sin tildes y sin ñ, y eso se
oía. Comprobado con `piper --debug`:

| escrito | fonemas que producía | correcto |
|---|---|---|
| `sueno` | `swˈeno` (no es una palabra) | `swˈeɲo` |
| `manana` | `manˈana` (no es una palabra) | `maɲˈana` |
| `clinico` | cli-**NI**-co | **CLÍ**-ni-co |
| `atencion` | a-**TEN**-cion | aten-**CIÓN** |
| `medica` | me-**DI**-ca, el verbo | **MÉ**-di-ca |
| `dirijase` | di-ri-**JA**-se | di-**RÍ**-ja-se |

Las tres últimas estaban en el cierre rojo, que es la frase que manda al paciente a
urgencias. `tests/test_ortografia_hablada.py` es la red: revisa las locuciones **y** recorre
una llamada completa por la máquina de estados, porque parte del habla se construye con
f-strings que ningún test de locuciones alcanza.

**2. Las cifras se dicen como las dice la gente** (`tts/hablado.py`). `38.5` se pronunciaba
"treinta y ocho **punto** cinco", y `123` —la línea de emergencias del cierre rojo— se
pronunciaba "**ciento veintitrés**", que es un número que el paciente no reconoce en el único
momento de la llamada en que eso importa. La capa solo actúa en la voz: el registro clínico y
la hoja de traspaso conservan la cifra.

**3. La cadencia era errática.** Medida sobre las 59 locuciones, la cola de silencio que
Piper deja iba de **200 a 785 ms** según la locución, así que el hueco entre dos frases del
mismo turno cambiaba de duración sin motivo. Ahora se recorta a una duración conocida y la
pausa entre fragmentos es deliberada y constante.

**4. El audio venía sin headroom.** Las 59 locuciones estaban a **pico 1.000** (0 dBFS) con
el RMS de voz variando **4.1 dB** entre ellas: cero margen y saltos de volumen percibido de
frase en frase. El techo se mide en el percentil 99.9 y no en el máximo, y eso no es un
detalle: usando el máximo, como las 59 estaban exactamente a 1.000, todas recibían la misma
reducción y el spread de RMS no bajaba nada.

| | antes | después |
|---|---:|---:|
| Locuciones pegadas a 0 dBFS | 59 | **0** |
| Spread de RMS entre locuciones | 4.1 dB | **1.2 dB** |
| Cola de silencio | 200–592 ms | **55 ms constante** |
| Variación de la cola | 392 ms | **0 ms** |

**Lo que no se movió, y por qué.** La velocidad global sigue en 1.0. Este modelo tiene ruido
de duración de fonema, así que dos síntesis del mismo texto ya difieren un 3 % entre sí:
medido con 4 repeticiones, `length_scale` 1.0 da 5.117 s y 1.05 da 5.083 s —indistinguible—
mientras 1.5 da 6.853 s. Un ajuste "algo más pausado" del 5 % no existe en la práctica, así
que se dejó en 1.0 con la variable `CENTINELA_TTS_VELOCIDAD` expuesta en vez de fingir una
mejora. La instrucción de urgencias **sí** va aparte, a 1.22 y con menos variabilidad de
fonema, porque ahí el margen sobre el ruido es real. `make muestras` genera el A/B para
juzgarlo por oído.

**El modelo solo cuando hace falta.** Si la regex extrajo el dolor y el léxico resolvió la
herida, no se invoca — de ahí que el P50 de invocaciones sea 0. La optimización quedó
registrada en `eval/probar_tokens.py`, sobre un escenario fijo de seis turnos: **1359 → 672
tokens de entrada por llamada**. Es un escenario fijo, distinto de la muestra de §5, así que
su cifra no tiene por qué coincidir con los 223.5 tokens/llamada de arriba: son dos
mediciones de dos cosas distintas y las dos son reales.

### Atribución cruzada: la cita es del paciente, no de la pregunta

En 2026 este modo de fallo tiene nombre: **deceptive grounding**. Una respuesta clínica
puede pasar todas las comprobaciones automáticas —cero alucinaciones, fidelidad al pasaje,
citas reales— y hablar de **la entidad equivocada**. Es invisible a las métricas de
fidelidad, porque cada afirmación viene de un documento real; lo que está mal es de quién
habla ese documento. Las tasas publicadas llegan al 87 % en condiciones adversas, y hasta
el 86.7 % en modelos afinados en biomedicina.

Aquí la entidad es el **procedimiento**, y el corpus entregado trae la trampa puesta:
`breast_cancer/` contiene 19 PDFs y todos son de cáncer de cuello uterino. Una ingesta que
se crea el nombre de la carpeta responde una pregunta de mastectomía citando
`002-GUIA-DE-CANCER-DE-CUELLO-UTERINO.pdf` — archivo real, carpeta correcta, enfermedad
equivocada, y ninguna métrica de fidelidad se queja.

`make atribucion` lo mide con **preguntas trampa**: cada una nombra la anatomía de otro
procedimiento y se le hace a quien no lo tiene. Un paciente de colecistectomía preguntando
cuándo puede doblar la rodilla operada. El recuperador tiene el material perfecto para esa
pregunta —las guías de artroplastia— y está prohibido usarlo.

| | |
|---|---:|
| Trampas | 10 |
| **Citas de otro procedimiento** | **0** |

Las diez trampas se resuelven de una de dos formas, y ninguna es un fallo. **Dos se
abstienen** con cero citas. Las otras responden con material de su propio procedimiento, y
en la mayoría el modelo dice que el dato específico no está ahí y redirige al equipo —
*«el contexto no proporciona información específica sobre la frecuencia de las
colonoscopias después de una colecistectomía»*. Es la conducta que se buscaba: **la
compuerta de fundamentación es generosa** —pasa con pasajes genéricos de postoperatorio— **y
la capa de respuesta es honesta.**

El reparto exacto entre esas dos formas se mueve de una corrida a otra, porque depende del
texto que el modelo genera; vive en `docs/metrics/atribucion.json` con la respuesta completa
al lado, y por eso **no se publica aquí como cifra**: un número que cambia sin que cambie el
sistema no es una medición, y ponerlo bajo `make cifras` sólo enseñaría a ignorar esa
comprobación. Lo que sí es invariante —y lo que la trampa mide— es la fila en negrita.

Y las dos trampas de mastectomía —control del cuello uterino, seguimiento por papiloma— se
abstienen con **cero citas**: el sistema no responde por los 19 PDFs cervicales que tiene
indexados, porque su tema lo decide el contenido y no la carpeta.

### Suite adversarial

`make redteam` corre 46 casos adversariales **contra el sistema completo**, no contra el
clasificador aislado. Resultado: **42/42**.

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
| **Parafraseo de bandera roja** | **10/10** |

- Intentos de manipulación resistidos: **11/11**
- Casos donde la criticidad bajó porque el paciente lo pidió: **0**

Más `make test`: **835 tests**, que incluyen cero falsos positivos de manipulación sobre
turnos textuales del dataset oficial. Ese grupo importa tanto como el primero: un agente
que acusa a un paciente asustado de intentar manipularlo es inservible.

### Turnarse como en una llamada

Dos cosas separan una llamada de un walkie-talkie: se puede cortar al otro, y no hay que
esperar a que se haga el silencio para que conteste.

**Interrumpir al agente.** `make bargein` mezcla las locuciones reales del agente —como
eco del altavoz— con las 18 grabaciones de voz humana de `eval/audios/`, a atenuaciones de
eco conocidas, y pasa la mezcla trama a trama por el mismo detector que corre en la llamada.

| Eco respecto a la voz | Detección | Baja la voz en (p50/p95) | Baches falsos/min | Cortes falsos |
|---|---:|---:|---:|---:|
| −30 dB · auriculares | 100 % | 112 / 144 ms | 0.00 | **0** |
| −20 dB · portátil con cancelación | 100 % | 80 / 144 ms | 0.00 | **0** |
| −12 dB · altavoz a volumen medio | 100 % | 144 / 202 ms | 0.21 | **0** |
| −6 dB · altavoz alto | 83 % | 208 / 342 ms | 0.21 | **0** |
| −3 dB · altavoz muy alto | 33 % | 240 / 1686 ms | 0.21 | **0** |

Dos capas, y la segunda es la que importa. La energía **sospecha** y el agente baja la voz
al 15 % en 20 ms; la transcripción del audio acumulado **confirma**, con las mismas puertas
de calidad que ya filtran las alucinaciones de Whisper. Así un falso positivo cuesta un
bache de 250 ms y no un turno perdido: **0 cortes falsos a cualquier nivel de eco**,
incluidos los dos que están fuera de lo exigible.

El umbral no es una constante: mientras el agente habla, lo que el micrófono oye *es* el eco
residual, y eso es una medida gratis del piso contra el que discriminar. Con auriculares
basta un susurro; con el altavoz alto hay que hablar más fuerte. `make bargein-barrido`
publica por qué el punto de operación está donde está, y las dos últimas filas de la tabla
publican dónde deja de funcionar: por encima de −6 dB el altavoz tapa al paciente y ningún
ajuste de umbral lo arregla.

**Contestar sin esperar el silencio.** El turno del paciente cerraba tras 900 ms de silencio,
fijos. Ahora 900 ms es el techo y no el plazo: si lo transcrito ya se sostiene solo —un
número para el dolor, una temperatura, un sí/no—, el turno cierra a los 450 ms. Medido con
`make escucha` sobre las 18 grabaciones reales: **11 de 18 cierran a 450 ms**, y las 7
restantes esperan el techo por un motivo que el arnés imprime, no por descarte. La métrica
que el paciente sufre no se movió: **0 de 18 turnos obligan a repreguntar**.

**Y cuánto se equivoca al cerrar antes**, que es el eje en el que el mercado compara los
detectores de fin de turno: LiveKit publica 9.9 % de cortes falsos con un presupuesto de
300 ms, frente al 27.7 % de un VAD acústico. La medición equivalente aquí no necesitó audio
nuevo — **una pausa a mitad de frase deja al servidor con un prefijo de la respuesta**, así
que `make cortes` recorre todos los prefijos de las 18 grabaciones y le pregunta al cierre
qué haría en cada uno.

Y la cifra que se publica es más estricta que la del mercado. Cerrar antes no siempre cuesta
algo: si el paciente ya dijo *«como un seis»* y lo que faltaba era *«…de dolor»*, el dato
clínico es el mismo y los 450 ms son gratis. El corte que duele es el que **cambia el dato
clínico**:

| | |
|---|---:|
| Respuestas con dominio abierto | 14 |
| Cierran en un prefijo | 3 |
| …y el dato no cambia | 3 |
| **Cortes falsos con costo clínico** | **0** |

Llegó a 0 arreglando un fallo que esta medición destapó, y era de los graves:

```
"Treinta y siete cinco"   ->  cerraba en "Treinta y siete"  ->  37.0 y no 37.5
```

**37.5 cumple la bandera amarilla de febrícula (≥ 37.4) y 37.0 no cumple nada.** El cierre
anticipado no perdía precisión: borraba la bandera. La causa es del idioma —en español la
décima se dice *después* del entero, «treinta y ocho *dos*»— así que un entero es un prefijo
tan válido como un valor final. Ahora una temperatura sin décima espera el techo, que es la
regla que este módulo ya declaraba en su propia documentación: *la duda se resuelve
escuchando más, nunca menos.* El precio, dicho: los cierres anticipados en prefijo bajaron de
5 a 3.

**No se añadió un detector aprendido, y conviene decir por qué.** El eslabón que limita el
piso de 450 ms no es la decisión —es la transcripción especulativa, que arranca a los
350 ms: por debajo de eso no hay texto que juzgar y el turno esperaría el techo igual. Un
modelo de fin de turno no movería ese límite. Lo que lo movería es arrancar la especulación
antes, y eso es una medición pendiente, no una que se pueda dar por hecha.

### Cuando el paciente se queda callado

La rúbrica lo pide dentro de *Calidad de la conversación*: «la latencia de la conversación
(...) y **qué hace tu solución durante los silencios**». Lo que hacía era nada. El VAD del
navegador no cerraba nunca, el turno quedaba abierto, y la llamada duraba hasta que el
barredor de inactividad la cerraba a los 180 s: tres minutos de nadie diciendo nada y un
registro clínico que no explicaba por qué.

**Callar es la primera respuesta correcta, no la ausencia de una.** Un paciente en el
tercer día de un postoperatorio piensa despacio —le duele, está medicado, y muchas veces
es mayor—, y pisar esa pausa es el error que la literatura de agentes de voz nombra por su
cuenta. Así que el primer peldaño de la escalera es esperar:

| Silencio | Qué hace | Por qué |
|---:|---|---|
| 0–6 s | nada | Es una pausa, no un silencio. |
| 6 s | *«Tómese su tiempo. Sigo aquí.»* | Quita presión; no la añade. |
| 14 s | repregunta lo mismo con otras palabras | Puede que la pregunta no se entendiera. |
| 25 s | *«No le escucho. ¿Sigue ahí?»* | Ya no parece que esté pensando. |
| 40 s | cierra, y deja constancia | La valoración quedó incompleta. |

Los plazos son **de silencio del paciente**: lo que el agente tarda en decir cada peldaño
no cuenta contra el siguiente. Medido con `make silencio`, la escalera completa sale en
**6.0 · 14.5 · 25.5 · 40.6 s** y cierra con motivo `silencio_del_paciente`.

Lo clínicamente importante es el último peldaño, y no es una decisión de conversación: **un
paciente que deja de responder no es una llamada que salió bien.** El motor ya cerraba en
AMARILLO cuando quedaban dominios sin preguntar —no se puede descartar lo que no se llegó
a preguntar— así que el silencio hereda esa conducta sin inventar una alerta nueva. Lo que
se añade es el motivo, y la hoja de traspaso abre con él:

```
=== SEGUIMIENTO ===

ATENCION: el paciente dejo de contestar a mitad de la llamada. La valoracion
quedo incompleta -- lo que aparezca abajo como NO REPORTADO no esta descartado,
esta sin preguntar.
```

Y lo que **no** hace: no escala a rojo por callarse. Un silencio no es un síntoma. Sin un
solo turno del paciente el cierre es el otro —`sin_contacto`, fuera de la bandeja clínica—
porque a alguien con quien no se habló no se le puede hacer triaje.

Esto destapó un fallo que llevaba tiempo ahí. `soltar_la_palabra()` solo se llamaba desde
la confirmación del cliente, y eso es correcto —el eco existe hasta que suena la última
muestra, y solo el cliente lo sabe— pero dejaba al servidor a merced de que el cliente
contestara. **Un navegador que se cuelga a media locución dejaba al agente con la palabra
para siempre**: sin eco que interpretar, sin turno posible, y con el vigilante de silencio
desactivado — la red que tenía que cubrir precisamente a quien no responde. Ahora el
servidor estima cuánto puede tardar en sonar lo que envió y suelta el suelo por su cuenta
si nadie confirma.

### Confirmar lo entendido, y aceptar una corrección

Un agente que solo pregunta y anota no conversa. Faltaban las dos cosas que hace cualquiera
al teléfono cuando el canal es dudoso, y el canal aquí lo es siempre.

**Leer de vuelta antes de escalar.** El reconocedor puede oír *«treinta y ocho»* donde el
paciente dijo *«treinta y seis»*, y el extractor puede convertir *«me duele bastante»* en un
siete que nadie dijo — `Procedencia.inferido` ya marcaba ese caso desde el primer día; lo que
faltaba era hacer algo con la marca. Así que antes de mandar a alguien a urgencias el agente
lo repite en castellano hablado: *«Le repito para estar seguro de que le entendí bien. Me
dice fiebre de 38.5 grados. ¿Es correcto?»*. En clínica esto se llama comunicación de
circuito cerrado y existe justamente porque el canal se equivoca.

Tres cosas que **no** cambian, y son las que hacen que esto sea seguro:

- **La alerta no espera la confirmación.** El ticket nace en el turno de la bandera, como
  antes. Si el paciente cuelga durante la confirmación, la alerta ya salió.
- **Un «no» no apaga el ticket.** Se anota el desmentido, se vuelve a preguntar el dominio, y
  decide una persona con el caso delante. Si un *«no, ya se me quitó»* pudiera retirar la
  bandera, la confirmación sería una puerta trasera a la criticidad — y el paciente que
  minimiza es uno de los perfiles del reto, no una hipótesis.
- **No conseguir confirmación no bloquea la instrucción.** Se pregunta una vez más y después
  se actúa sobre lo entendido. Alguien con 38.5 que no contesta ni sí ni no tiene que oír que
  vaya a urgencias igual.

**Corregirse a mitad de llamada.** *«No, dije 38.5, no 35.8.»* Antes el valor se sobrescribía
en silencio: se perdía la versión anterior y se perdía el hecho de que hubo una corrección,
que es un dato clínico por sí mismo. Ahora el agente lo acusa en voz alta y repite el valor
nuevo, y la hoja de traspaso lleva las dos versiones con su turno. Bajar la cifra tampoco
retira la alerta: dentro de una llamada la criticidad **solo puede subir**
(`_con_piso_de_criticidad`), y `make redteam` lo comprueba con un caso que establece la
bandera y después intenta desdecirse.

**Que la instrucción de urgencias se oiga entera.** Es la única frase del sistema cuya
pérdida es un daño clínico, y con barge-in el paciente puede cortarla. Si la corta, la llamada
no cuelga: el agente cede la palabra y la repite una vez. Si tampoco oye la repetición, el
registro lo dice —`urgencia_oida` en falso— y los próximos pasos de la hoja cambian: ya no
dicen «verificar que el paciente llegó», dicen que hay que darle la indicación por teléfono.
Un rojo que el paciente no llegó a oír no es el mismo rojo.

**Que un bache de red no cueste la llamada.** El WebSocket se cae y el navegador vuelve dentro
de una ventana de gracia de 20 s: la llamada sigue con su estado clínico y su dominio abierto.
Antes, un wifi que cambiaba de banda cerraba la llamada como «interrumpida» y el paciente
empezaba de cero. Ninguna garantía se debilitó: el cierre forzado sigue ocurriendo cuando
nadie vuelve, solo se retrasa hasta que la ventana expira (`make humo` casos 10 y 12).

### Compuerta de arranque (G2)

`scripts/ensayo_g2.ps1` clona el repo en un directorio limpio y cronometra los pasos del
README, uno por uno.

| Paso | Tiempo |
|---|---:|
| Clonar el repositorio (116 MB, desde el remoto público) | 12.4 s |
| `make instalar` | 14.8 s |
| `make ollama` (modelo ya descargado) | 0.1 s |
| `make piper` (con descargas en caché) | 0.8 s |
| `make modelos` (con descargas en caché) | 5.0 s |
| Extraer el índice del corpus | 1.3 s |
| `make up` hasta la primera respuesta | 16.5 s |
| Verificación funcional (llamada + caso rojo + consola + voz clonada en el clon) | 1.4 s |
| **Total medido** | **0.9 min** |
| **Peor caso estimado en máquina virgen** (+4.7 GB de descargas) | **5.7 min** |

El límite del reto son 15 minutos. El margen en el peor caso es de **9.3 minutos**.

### Compuerta de conocimiento vivo (G5)

*«Subes un documento desde tu consola de administración y el agente lo usa; lo eliminas y
el agente lo olvida. Se verifica con un documento de prueba que no forma parte de ningún
corpus entregado.»*

`make humo` caso 7 recorre el ciclo entero con un documento inventado: no está antes, se
ingiere, se recupera, se cita, se borra, y el borrado emite un **recibo de olvido** que
vuelve a lanzar la consulta y prueba que la cita desapareció. Al leer la compuerta con
calma aparecieron dos huecos, y los dos la habrían costado:

**Solo se aceptaba `.pdf`.** El corpus del reto son PDFs y la ingesta se escribió para
eso, pero la compuerta no dice PDF — y quien fabrica un documento de prueba con un dato
inventado lo más rápido es escribir un `.txt`. La respuesta era `400 solo se aceptan
archivos PDF`. Ahora entra también texto plano, partido en páginas sintéticas por límite
de párrafo, porque la cita necesita una página que se pueda verificar contra la fuente.

**Y lo subido no se veía en las llamadas.** El filtro por tema —lo que impide citar el
procedimiento equivocado— excluye todo lo que no coincida con el procedimiento del
paciente. Un documento cuyo texto no nombra ninguno de los cinco no recibe tema, así que
no coincidía con nada: **en una llamada quedaba invisible, y en una llamada es donde la
compuerta dice que el agente lo usa.** El jurado lo habría subido, lo habría visto
indexado, y no habría pasado nada.

El arreglo tuvo dos versiones y la primera estaba mal, de una forma instructiva. Dejar
entrar *cualquier* documento sin tema también dejaba entrar el PDF del corpus que el
clasificador no supo etiquetar, y la abstención de mastectomía pasó de *«no la tengo»* a
una respuesta fundamentada con cinco citas: **+11 preguntas respondidas que en realidad
eran 11 abstenciones perdidas.** La regla correcta es más estrecha — lo que entró por la
consola, no lo que no tiene tema — porque un documento que un operador acaba de subir
mientras la llamada corre y un PDF que el clasificador no entendió no son la misma cosa.

Con eso, el caso 7 comprueba además lo que la compuerta pide textualmente: el agente cita
el documento nuevo **dentro de una llamada**, con el filtro por procedimiento activo, y
deja de citarlo en el turno siguiente al borrado.

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

### 2. El corpus tiene un hueco clínico de 8 pacientes, y estaba puesto a propósito

`dataset/textos/breast_cancer/` contiene 19 PDFs. **Ninguno es de cáncer de mama: todos
son de cáncer de cuello uterino.** Mientras tanto, `perfiles_clinicos` tiene 8 pacientes
(20 % de la población) con procedimiento **Mastectomía**.

Lo preguntamos a la organización. Respondieron el **2026-08-08** que el desajuste era
**intencional**, puesto ahí *"para evaluar el criterio y la capacidad analítica de cada
concursante"*, y que sobre complementar con guía pública marcada *"el enfoque correcto debe
ser corregir y ajustar el modelo en sí"*.

Así que lo que se evalúa no es conseguir documentos de mastectomía: es que el sistema
detecte que no tiene cobertura y **se niegue a responder**. Eso es lo que hace, y por dos
caminos que no dependen del nombre de la carpeta.

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

**Y es el hueco entero de la medición.** De las 60 preguntas de `make rag`, las 12 que no se
responden con el corpus oficial son *exactamente* las de mastectomía; los otros cuatro
procedimientos van 12/12.

La respuesta del agente ahí es abstenerse, con la razón registrada:

> *"Esa es una buena pregunta y le voy a ser honesto: no la tengo en la información clínica
> que manejo. Prefiero decírselo así que darle un dato que no me consta. La dejo anotada
> para que el equipo clínico se la responda."*
>
> `razón: el corpus cargado no cubre el procedimiento del paciente (tema requerido:
> cancer_mama). Responder con otro tema sería clínicamente inseguro.`

Queda **un** documento complementario (`scripts/ingerir_complementario.py`, 23 fragmentos de
Memorial Sloan Kettering con su URL y fecha en `data/complementario/procedencia.json`), y
está ahí como demostración de que el sistema puede ingerir material externo con procedencia
declarada y que la categoría `complementario` viaja hasta la cita: cuando el agente se apoya
en ese material, la respuesta lo dice. No se puede hacer pasar material añadido por material
entregado.

Por eso **`make rag` publica dos cifras y no una**:

| Respaldo de las 60 respuestas | Fundamentadas |
|---|---:|
| Solo con el corpus **oficial** del reto | **48 / 60** |
| Apoyadas en material complementario declarado | 1 / 60 |
| Abstenciones honestas, con la razón registrada | **11** |
| Citas cruzadas de otro procedimiento · cifras sin respaldo | **0 · 0** |

Sumar documentos que responden nuestras propias preguntas de evaluación y publicar una sola
cifra sería inflar la medición. Y `make auditar` sigue diciendo *Mastectomía → 0 docs, SIN
COBERTURA* sobre el corpus entregado: el hallazgo no se borra.

**Reducir el complemento destapó un defecto de la compuerta, y es el hallazgo más útil de
esta parte.** Con un solo folleto de alcance estrecho, la similitud coseno dejó de
discriminar —las siete preguntas de prueba caen entre 0.843 y 0.883, todas por encima del
umbral de 0.82— mientras el solape léxico separaba limpio: 0.00–0.25 lo que el folleto no
cubre, 0.50–0.75 lo que sí. Como la compuerta exigía *similitud **o** solape*, la similitud
daba luz verde siempre y la señal útil no llegaba a contar. El síntoma medido: a *"¿qué hago
si me sale líquido de la herida?"* el agente respondía *"bombee con el puño lentamente 10
veces"*, que es un ejercicio de linfedema. Ahora, cuando **todo** el soporte es
complementario se exigen las dos señales (`rag/retriever.py`,
`MIN_SOLAPE_COMPLEMENTARIO = 0.38`, calibrado entre esas dos bandas). El corpus oficial
conserva el criterio permisivo, que es el correcto cuando la fuente cubre el tema entero.

Otro PDF se descargó y **se rechazó**: era de reducción mamaria, no de mastectomía. Lo
detectó la misma clasificación por contenido que denuncia el defecto del corpus oficial —el
script se niega a ingerir lo que no clasifique como `cancer_mama`—, porque repetir en el
material propio el error que se le señala al entregado sería lo peor de los dos mundos.

Y un segundo documento complementario, la guía de Fred Hutchinson, **se retiró** del índice
a `data/complementario/retirados/`: aportaba 139 fragmentos y 2.3 MB para cubrir un hueco
que la organización confirmó intencional y que se evalúa por la abstención, no por la
cobertura. El retiro dejó recibo de olvido verificado (`olvido_verificado: true`).

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
tipografías (Montserrat y Chivo Mono, SIL OFL) van **versionadas en el repo** en
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
descartado (ver hallazgo 1). Nombre y versión exactos, que es lo que la compuerta pide
declarar.

La regla vigente son **familias**, no versiones puntuales: la organización enmendó
[`docs/stack-tecnico.md`](https://github.com/TechSphere2026/ParticipantArtifacts/blob/main/docs/stack-tecnico.md)
el 2026-08-07 (commit `5811f6f`) porque los proveedores retiran snapshots sin avisar. Phi-3.5
Mini 3.8B pertenece a **Microsoft Phi Mini (serie 3.5+, ~3–4 B), local**, y Llama 3.2 1B a
**Meta Llama (serie 3.x, 1B–3B), local**: las dos familias están permitidas.

La enmienda permite además usar el sucesor vigente de la misma familia, así que la pregunta
dejó de ser retórica: **Phi-4-mini es ese sucesor.** `make ab-modelo` lo mide — cada modelo
con su propio servidor, porque el modelo se resuelve al arrancar el proceso y comparar dos
con un solo servidor mide uno dos veces:

| | Phi-3.5 Mini (vigente) | Phi-4-mini |
|---|---:|---:|
| 160 casos oficiales | 152/160 | 152/160 |
| Falsos negativos clínicos | 0 | 0 |
| Suite adversarial | **43/43** | **41/43** |
| …paráfrasis coloquial de bandera roja | **10/10** | **8/10** |
| Manipulación resistida | 12/12 | 12/12 |
| Segundos de la suite | **39.7** | 86.6 |

Dos filas no se mueven y eso es el resultado, no el ruido: **el replay de los 160 casos y la
resistencia a manipulación son idénticos porque los decide código**, no el modelo. Es la
tesis del proyecto medida por accidente — cambiar el modelo de razonamiento no mueve la
decisión clínica.

Donde se separan es en la percepción, que es lo que el modelo sí hace. Phi-4-mini falla **2
de 10** paráfrasis coloquiales de bandera roja: *«de la cortada sale un líquido gruesito,
entre amarillo y…»*, *«le tuve que cambiar la gasa tres veces porque se empapa»*. Eso es un
falso negativo clínico, el fallo que la rúbrica pesa por encima de todo. Y de paso tarda
**2.2 veces más**.

Así que se queda Phi-3.5 Mini, y ahora por una medición y no por inercia.

Se verifica en `api/centinela/config.py`, en el `Makefile` y en `GET /api/salud`.

---

## Documentación

| Documento | Qué contiene |
|---|---|
| [`docs/informe-final.md`](docs/informe-final.md) | Informe final: declaración del modelo, decisión técnica, riesgos, proceso |
| [`docs/arquitectura.md`](docs/arquitectura.md) | Diagramas de arquitectura y flujo de decisión, verificados contra el código |
| [`docs/metricas.md`](docs/metricas.md) | Todas las métricas medidas, generadas por script |
| [`docs/informe-corpus.md`](docs/informe-corpus.md) | Auditoría de integridad del corpus entregado |
| [`docs/operacion.md`](docs/operacion.md) | Runbook: configuración, garantías, qué hacer cuando una alerta no sale, respaldo, retención |
| [`prompts/`](prompts/) | Prompts versionados, uno por archivo, con su esquema y notas |

## Verificar la entrega

Las siete primeras también se pueden lanzar desde la pestaña **Pruebas** del panel, que
ejecuta exactamente estos comandos como subprocesos. El veredicto es su código de salida,
así que no hay una segunda implementación dentro del panel.

```bash
make test        # 835 tests unitarios y de regresión
make eval        # 160 casos oficiales · cero falsos negativos clínicos
make redteam     # 46 casos adversariales (requiere la API levantada)
make humo        # 103 comprobaciones de extremo a extremo (requiere la API levantada)
make rag         # 60 preguntas · 0 citas cruzadas, 0 cifras sin respaldo
make bargein     # 0 cortes falsos y latencia de interrupción medida
make escucha     # cierre del turno y palabra clínica sobre 18 grabaciones reales
make tendencia   # barrido de tendencia sobre las 40 trayectorias oficiales
make cifras      # comprueba que los números de estos documentos siguen siendo ciertos
make diagrama    # cada elemento del diagrama existe en el código
make auditar     # defectos detectados en el corpus entregado
make bench       # latencia de modelo y voz
make g2          # ensayo cronometrado de la compuerta de arranque
make metricas    # regenera docs/metricas.md desde las mediciones
```

---

## Licencia

**MIT**, con el texto completo en [`LICENSE`](LICENSE). Los PDFs del corpus clínico son obra de sus respectivos autores y se usan solo como
material de referencia del reto. Lo mismo vale para el material complementario de
`data/complementario/`: es una guía de educación al paciente publicada por Memorial Sloan
Kettering, con su URL y su fecha de descarga registradas en `procedencia.json`, y se usa
igual — como material de referencia, sin reclamar ningún derecho sobre ella. La de Fred
Hutchinson Cancer Center / UW Medicine, retirada del índice, queda en `retirados/` con la
misma procedencia registrada.

Los datos clínicos del dataset son **sintéticos y no han sido validados clínicamente**.
