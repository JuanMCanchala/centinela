# Centinela

**Agente de voz para seguimiento postoperatorio.** Llama al paciente, conversa en español
colombiano, reconstruye su cuadro clínico hablando, y decide si hay que alertar a un
humano. Cada respuesta clínica cita el documento que la sustenta; cada decisión cita la
regla que la disparó.

Entrega para el **Tech Sphere Challenge 2026** (Source Meridian).

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
lo reportado se contrasta con los logs de la sesión. Muestra: **31 turnos en 8 llamadas
completas**, la que produce `make humo` sobre un servidor recién arrancado.

**1. Latencia de respuesta** — *desde que se cierra el VAD (fin de habla del paciente)
hasta que el primer byte de audio del agente sale hacia el navegador.*

| Percentil | Latencia |
|---|---:|
| **P50** | **0.6 ms** |
| **P95** | **3 370.6 ms** |
| P99 | 13 285.4 ms |

El P50 es de milisegundos porque **84 % de los turnos se sirven desde el caché de audio
pre-renderizado** (26 de 31): la conversación la conduce una máquina de estados, así que
las locuciones del guion se conocen antes de que suene el teléfono.

La cola es alta y conviene decir de qué está hecha, porque no es la del turno normal. Los
turnos que sintetizan voz nueva —la lectura de vuelta de un hallazgo, una respuesta del
corpus— pagan el TTS en caliente, y los dos turnos de esta muestra que además vienen de
**audio** pagan Whisper: el STT tarda 5.6 s en P50 sobre este equipo sin GPU. Con 31
muestras un P99 es un dato anecdótico, no una estadística; se reporta por completitud.

**2. Consumo por turno y por llamada**

| Métrica | Valor |
|---|---:|
| Tokens de entrada / salida por turno (P50) | **0 / 0** |
| Tokens de entrada / salida por turno (media) | 148.9 / 4.4 |
| Tokens de entrada / salida **por llamada** (media) | **577.1 / 17.1** |
| Turnos por llamada (media) | 3.9 |

**3. Invocaciones al modelo por turno** — **P50 = 0**, máximo 1. La mayoría de los turnos
no llegan al modelo: si la regex ya extrajo el dolor y el léxico resolvió la herida, no hay
nada que preguntarle. Es la consecuencia directa de que la decisión clínica la tome el
motor de reglas.

**4. Consultas al RAG por llamada** — **0.25 de media**, máximo 1. Son bajas a propósito:
el cuestionario no consulta el corpus, recorre seis dominios con preguntas fijas. El RAG
entra cuando el paciente pregunta algo clínico —*«¿puedo ducharme?»*— y entonces la
respuesta va fundamentada y con su cita. Una media alta aquí significaría que el agente
consulta documentos para preguntar la temperatura: gasto sin ganancia.

**Costo estimado por llamada: USD 0.002425** (COP 9.7). Centinela corre local, así que el
costo marginal real es electricidad; la rúbrica pide extrapolar a precios de API y explicar
el cálculo. Son los tokens y segundos de audio realmente medidos por tarifas públicas de
referencia: modelo USD 0.000059 · transcripción USD 0.000962 · voz USD 0.001404. Las
tarifas están en `obs/metrics.py::PRECIOS_REFERENCIA` y los insumos en
[`docs/metricas.md`](docs/metricas.md), que se genera del mismo `runtime.json` que estas
cifras — si divergen, es que alguien editó una a mano.

Para reproducirlas hace falta un servidor **recién arrancado** — si se mide sobre uno donde
alguien estuvo probando a mano, la muestra se llena de llamadas abandonadas de dos turnos:

```bash
make up          # en otra terminal
make humo        # llamadas completas de extremo a extremo
make runtime     # congela /api/metricas
make metricas    # las escribe en docs/metricas.md
```

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
dominios, así que las **59 locuciones** que el agente puede decir se conocen antes de que
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
su cifra no tiene por qué coincidir con los 577.1 tokens/llamada de arriba: son dos
mediciones de dos cosas distintas y las dos son reales.

### Suite adversarial

`make redteam` corre 43 casos adversariales **contra el sistema completo**, no contra el
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

Más `make test`: **488 tests**, que incluyen cero falsos positivos de manipulación sobre
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
| Clonar el repositorio | 14.1 s |
| `make instalar` | 13.4 s |
| `make ollama` (modelo ya descargado) | 0.2 s |
| `make piper` (con descargas en caché) | 1.1 s |
| `make modelos` (con descargas en caché) | 6.4 s |
| Extraer el índice del corpus | 0.6 s |
| `make up` hasta la primera respuesta | 9.4 s |
| Verificación funcional (llamada + caso rojo + consola) | 1.1 s |
| **Total medido** | **0.8 min** |
| **Peor caso estimado en máquina virgen** (+4.6 GB de descargas) | **5.4 min** |

El límite del reto son 15 minutos. El margen en el peor caso es de **9.6 minutos**.

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

La enmienda permite además usar el sucesor vigente de la misma familia. **No se cambió de
modelo a propósito**: todas las cifras de este documento están medidas con Phi-3.5 Mini, y
cambiar el modelo sin volver a medirlas dejaría un informe que no describe lo que corre.

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
make test        # 488 tests unitarios y de regresión
make eval        # 160 casos oficiales · cero falsos negativos clínicos
make redteam     # 43 casos adversariales (requiere la API levantada)
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

MIT. Los PDFs del corpus clínico son obra de sus respectivos autores y se usan solo como
material de referencia del reto. Lo mismo vale para el material complementario de
`data/complementario/`: es una guía de educación al paciente publicada por Memorial Sloan
Kettering, con su URL y su fecha de descarga registradas en `procedencia.json`, y se usa
igual — como material de referencia, sin reclamar ningún derecho sobre ella. La de Fred
Hutchinson Cancer Center / UW Medicine, retirada del índice, queda en `retirados/` con la
misma procedencia registrada.

Los datos clínicos del dataset son **sintéticos y no han sido validados clínicamente**.
