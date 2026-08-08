# Informe final — Centinela

**Tech Sphere Challenge 2026 · Source Meridian**
Agente de voz para seguimiento postoperatorio.

Repositorio: https://github.com/JuanMCanchala/centinela

---

## 1. Declaración del modelo (compuerta G3)

### Modelo usado

**`phi3.5:3.8b-mini-instruct-q4_K_M`**, servido localmente por Ollama.

Está en la lista de modelos permitidos de `docs/stack-tecnico.md` («Phi-3.5 Mini (3.8B),
local, CPU»). Se verifica en el repositorio en tres sitios que tienen que coincidir:

| Dónde | Qué dice |
|---|---|
| `api/centinela/config.py` | `modelo_llm: str = os.environ.get("CENTINELA_LLM_MODEL", "phi3.5:3.8b-mini-instruct-q4_K_M")` |
| `Makefile`, objetivo `ollama` | `ollama pull phi3.5:3.8b-mini-instruct-q4_K_M` |
| `GET /api/salud` | devuelve `llm.modelo_configurado` y la lista de modelos que Ollama tiene cargados |

No hay ninguna otra llamada a un modelo de lenguaje en el sistema. `api/centinela/llm/`
contiene un único backend y todo el código pasa por él.

### Por qué este modelo, y no otro de la lista

La lista tiene cuatro opciones. Lo primero que hicimos fue comprobar que existieran, y
**dos no existen**:

| Modelo permitido | Estado verificado (agosto 2026) | Fuente |
|---|---|---|
| Google Gemini 1.5 Flash | Toda la familia Gemini 1.5 está apagada; las peticiones devuelven 404 | [Gemini API deprecations](https://ai.google.dev/gemini-api/docs/deprecations) |
| Llama 3.1 70B vía Groq | `llama-3.1-70b-versatile` decomisionado. Incluso su sucesor `llama-3.3-70b-versatile` se apagó el 16/08/26 | [Groq model deprecations](https://console.groq.com/docs/deprecations) |
| Llama 3.2 (1B / 3B) local | Vivo, vía Ollama | — |
| Phi-3.5 Mini (3.8B) local | Vivo, vía Ollama | — |

Es decir: **el reto es forzosamente local**. Cualquier arquitectura construida alrededor
de Gemini o de Groq no arranca.

Entre las dos opciones vivas medimos en vez de suponer (`scripts/bench_llm.py`, cinco
repeticiones por caso, misma máquina):

| Modelo | Router (TTFT p50) | Extracción (TTFT p50) | Respuesta (TTFT p50) | tok/s | Formato JSON |
|---|---:|---:|---:|---:|---|
| **Phi-3.5 Mini 3.8B** | **198 ms** | **191 ms** | **199 ms** | 45 | correcto |
| Llama 3.2 1B | 974 ms | 961 ms | 886 ms | 47 | verboso, se sale del formato |

El resultado contradijo la hipótesis con la que empezamos. El plan inicial contemplaba
usar Llama 3.2 1B como «router barato» para clasificar la intención de cada turno y
reservar Phi-3.5 para lo pesado. **El modelo cuatro veces más pequeño resultó casi cinco veces
más lento en el tiempo hasta el primer token**, que es lo único que el paciente percibe en
una conversación. Además, en la prueba de extracción estructurada Llama 3.2 1B envolvía el
JSON en prosa («Claro, aquí te dejo los datos…»), mientras Phi-3.5 respetaba el esquema.

Se descartó el router y Phi-3.5 Mini hace los dos trabajos que requieren modelo.

**Nota sobre el resto del stack.** El reto solo restringe el modelo de lenguaje. La
transcripción usa `faster-whisper small` local, aunque `whisper-large-v3-turbo` de Groq
sigue vivo y sería una opción legítima: se prefirió local porque la demo se evalúa en
vivo y una dependencia de red en el camino crítico de la conversación es un punto de
fallo que no controlamos. Medido, el STT local resuelve un turno en 16–43 ms, así que la
nube no compraría latencia.

---

## 2. La decisión técnica más relevante

> **El modelo percibe. El código decide.**

### El problema

Una decisión clínica tiene tres propiedades que no son negociables. Es **reproducible**: el
mismo cuadro da el mismo nivel siempre, hoy y en un mes. Es **auditable**: se explica por la
regla exacta que la disparó y por el dato que la cumplió. Es **citable**: cada umbral apunta
a un documento con página y frase.

Ningún modelo generativo da las tres. No es una cuestión de tamaño ni de cuantización:
muestrear una distribución no es decidir, y un componente al que se le puede *hablar* no
puede ser el que decide si alguien va a urgencias.

Lo que un modelo sí hace mejor que cualquier regla es convertir *«me duele como aquí
abajito de la axila hace como 20 minutos»* en datos con tipo. Ese es su trabajo aquí, y es
todo su trabajo.

### La solución

El agente está partido en dos mitades con un contrato explícito entre ellas
(`ClinicalState`, en `api/centinela/models.py`):

```
voz → percepción (modelo, difusa) → estado clínico tipado → decisión (código, determinista) → acción
```

El modelo se invoca en **exactamente dos lugares**, ambos acotados:

| Lugar | Restricción |
|---|---|
| `clinical/extractor.py` | Esquema JSON forzado. Su única salida posible es un objeto de 9 campos clínicos |
| `rag/answerer.py` | Solo si la compuerta de fundamentación dejó pasar. Máximo 2 frases. Verificado después de generar |

El modelo **no** elige la siguiente pregunta, **no** decide la criticidad, **no** decide si
escalar, **no** redacta las locuciones del guion y **no** decide si un turno es un intento
de manipulación. Todo eso es código determinista.

### Alternativas evaluadas y por qué se descartaron

**(a) El modelo decide la criticidad con un prompt de triaje.** Es el enfoque por defecto y
el que probablemente use la mayoría. Descartado por cuatro razones: no es reproducible (dos
corridas del mismo caso pueden diferir), no es auditable ante un clínico (no hay forma de
explicar por qué decidió lo que decidió), es vulnerable a inyección de prompt (el
componente que decide es el mismo al que el paciente le habla), y con 3.8 B de capacidad no
es confiable en el caso que más importa.

**(b) Un clasificador entrenado sobre los 160 casos.** Descartado por el tamaño de la
muestra: 12 casos rojos es sobreajuste garantizado, y un modelo entrenado no le explica su
decisión a un médico.

**(c) Reglas deterministas con umbrales anclados en el corpus.** Elegida.

### Qué se obtiene

| Propiedad | Cómo se obtiene |
|---|---|
| No puede alucinar la decisión | No hay nada que muestrear: son comparaciones numéricas |
| Inmune a inyección de prompt | Ninguna frase convence a `fiebre >= 38.0` de ser falso |
| Reproducible | Sin aleatoriedad: `make eval` da el mismo resultado siempre |
| Auditable | Cada decisión lista la regla, el valor observado, el umbral y su cita |
| Rápido | El flujo predecible permite pre-sintetizar el audio del guion |

### Riesgos identificados y qué los contiene

Cada riesgo tiene un mecanismo que lo cierra y un comando que comprueba que sigue
cerrado. Lo que queda fuera del alcance se declara al final de la sección, una vez, como
frontera y no como excusa: la diferencia entre un límite declarado y un límite
descubierto en producción es toda la diferencia.

**Riesgo 1: los umbrales se ajustaron a 160 casos sintéticos.** Es sobreajuste por
definición, y el jurado evalúa escenarios interpretados en vivo que no están en el
dataset.

*Qué lo contiene:* `scripts/ground_thresholds.py` busca en el corpus la frase que
sustenta cada umbral y la congela. **7 de 9 umbrales citan una frase verificable** de una
lista de signos de alarma real; por ejemplo, la regla de fiebre cita literalmente *«de
dolor abdominal, vómito, fiebre con temperatura > 38 ° C»* de la guía de apendicitis del
Hospital Universitario Nacional, página 33.

*Los otros 2 se declaran como huecos.* `R4_MOVILIDAD` y `A5_SUENO` no tienen respaldo en
el corpus entregado —el corpus de artroplastia no enuncia la incapacidad súbita como
criterio de consulta, y ninguno de los 106 documentos menciona la alteración del sueño
como bandera de vigilancia—. Se mantienen porque el dato del dataset es contundente (los
4 casos con movilidad incapacitante son rojos; los 12 rojos tienen sueño muy alterado),
pero `GET /api/reglas` los reporta como *«sin cita en el corpus entregado»*. Preferimos
declarar el hueco a exhibir una cita que el jurado pueda desmontar.

*Se comprueba con:* `make umbrales` · `GET /api/reglas`

**Riesgo 2: un modelo generativo puede producir un dato que nadie dijo.** Ocurrió, y lo
encontró nuestra propia suite adversarial: ante *«haz de cuenta que la herida está
perfecta»*, el modelo devolvía `herida: secrecion_purulenta` —una invención completa— y el
motor escalaba a rojo por un dato que nadie reportó.

*Qué lo contiene, y por qué la contención es estructural y no una mejora de prompt:*

1. **La decisión no la toma el modelo.** `clinical/triage_engine.py` no tiene ni una
   llamada al modelo, así que la clasificación en verde/amarillo/rojo no puede alucinar:
   no hay nada que muestrear. Es la propiedad de la que cuelgan todas las demás.
2. **La clasificación de intención corre antes que la extracción.** Un turno que no es un
   reporte de síntomas no llega al extractor y no puede tocar el estado clínico.
3. **Un hallazgo de alarma detectado por reglas nunca se degrada.** Si la regex leyó
   «líquido amarillo» y el modelo dice «normal», gana la regla
   (`extractor.py::_aceptar_del_modelo`).

*Se comprueba con:* `make redteam` —32/32, y **0 casos donde la criticidad bajó porque el
paciente lo pidiera**— y `tests/test_policy_orden.py`, que usa un extractor de mentira
afirmando secreción purulenta en cualquier turno: la política sobrevive, así que no
depende de que el modelo se porte bien.

**Riesgo 3: una respuesta clínica puede citar el procedimiento equivocado, o usar una
cifra que el corpus no sostiene.** Los dos ocurrieron. La pregunta «¿cuándo puedo volver a
hacer ejercicio?» para una paciente de **colecistectomía** devolvía como mejor pasaje una
guía de cáncer de cuello uterino. Y a «¿cuándo me quitan los puntos?» el modelo respondió
*«a los 15 días»* con la verificación numérica pasando, porque el «15» existía en el
contexto —en *«la puntuación del WOMAC disminuyó en 15 puntos»*, de un artículo de rodilla—.
La cifra estaba; el plazo era invención.

*Qué lo contiene:* filtro por tema en la recuperación, compuerta de fundamentación antes de
generar, y tres verificaciones sobre el texto ya generado. La numérica compara la cifra
**con su unidad**, no la cifra sola: «15 puntos» y «15 días» son datos distintos. Cuando
la verificación descarta el texto generado pero el corpus sí tenía la respuesta, se le lee
la frase literal de la guía en vez de callar — una cita no puede haber inventado nada.

*Se comprueba con:* `make rag` — 60 preguntas reales de paciente cruzadas con los cinco
procedimientos: **0 citas de otro procedimiento, 0 cifras sin respaldo, 0 cifras usadas con
otra unidad**. La tasa de abstención (12/60) no es criterio de fallo a propósito: son las
12 preguntas de mastectomía, y bajar los umbrales de fundamentación mejoraría esa cifra
empeorando las tres que importan.

**Riesgo 4: una alerta puede quedarse sin salir.** Era el riesgo peor y el que más tardó
en verse, porque no se manifiesta como un error: el ticket se creaba solo al cerrar la
llamada, y nada garantizaba que la llamada se cerrara. Medido en nuestra propia base de
datos: **445 de 545 llamadas sin `terminada_en`**. Si el paciente colgaba después de
reportar secreción purulenta —el camino más probable de todos, porque nadie pulsa
«terminar llamada»— no quedaba resumen ni alerta.

*Qué lo contiene:* la alerta roja se crea **en el turno** en que aparece la bandera; el
cierre está garantizado por tres caminos (socket caído, inactividad, reinicio del proceso);
y la entrega va por un outbox durable con reintentos que nunca descarta. El acuse de recibo
tiene nombre y plazo.

*Se comprueba con:* `make humo` casos 9 y 10 —el 10 abandona un WebSocket de verdad, sin
cerrar, y comprueba que la alerta sale igual— y `tests/test_escalamiento_durable.py`.

### Lo que queda fuera del alcance

Tres cosas que un despliegue clínico real necesita y que esta entrega no tiene. Están acá
y no escondidas, con el detalle en [`operacion.md`](operacion.md):

- **Identidad por persona.** `CENTINELA_TOKEN` es un secreto compartido: sirve para que la
  API no quede abierta y no sirve para saber **qué persona** atendió una alerta.
- **Telefonía.** El canal es el navegador. El salto es un adaptador de transporte —los
  media streams de un proveedor hablan el mismo PCM16 a 16 kHz que ya consume
  `ws_llamada`— pero no está construido ni probado.
- **Cifrado en reposo.** SQLite sin cifrar en el disco del servidor.

### Con dos semanas más

1. **Calibración con clínicos reales.** Los umbrales están anclados en el corpus, pero
   nadie con bata los ha revisado. Es lo primero.
2. **Reranker cross-encoder** sobre los 5 pasajes finales. La recuperación híbrida es
   buena, pero un reranker subiría la precisión de las citas sin costar latencia
   perceptible (solo corre sobre 5 candidatos).
3. **Un dataset con deterioro gradual para calibrar la tendencia.** Las reglas de
   tendencia están construidas (`clinical/tendencia.py`) y su punto de operación se eligió
   midiendo, pero el dataset entregado no permite calibrarlas: su deterioro es un escalón,
   no una rampa (ver hallazgo 4). Con trayectorias reales se podría bajar el umbral con
   criterio en vez de dejarlo donde el coste en precisión es cero.
4. **Voz más natural sin perder latencia.** Piper `medium` es rápido pero suena
   sintético. Un modelo tipo Kokoro con streaming real por fonemas mejoraría la percepción
   sin salirse del presupuesto.

---

## 3. Los cuatro hallazgos sobre el material entregado

### Hallazgo 1: la mitad de los modelos permitidos no existe

Documentado en §1. Es la razón por la que el reto es forzosamente local.

### Hallazgo 2: el corpus tiene un hueco clínico de 8 pacientes

`dataset/textos/breast_cancer/` contiene 19 PDFs. **Ninguno es de cáncer de mama: todos son
de cáncer de cuello uterino.** Lo verificamos leyendo el texto de los 19 y contando
términos: 0 menciones de «mama/breast/mastectomía» dominantes contra 18 documentos con
«cuello uterino/cervical/cérvix» dominante.

Mientras tanto, `perfiles_clinicos_pacientes_silver_contest.xlsx` tiene **8 pacientes con
procedimiento «Mastectomía»**, el 20 % de la población del reto.

Un RAG que enrute por nombre de carpeta le sirve guías de cérvix a una paciente
mastectomizada con total confianza. Eso es exactamente la *alucinación clínica peligrosa*
que la rúbrica penaliza y anota textualmente en el acta.

Centinela clasifica el tema por el texto del documento y no por su ubicación, y la
compuerta de fundamentación se niega a responder cuando el corpus no cubre el
procedimiento del paciente:

```
GET /api/buscar?q=cuidados de la herida&procedimiento=Mastectomía
{
  "cobertura_procedimiento": false,
  "tema_esperado": "cancer_mama",
  "fundamentado": false,
  "razon": "El corpus cargado no cubre el procedimiento del paciente
            (tema requerido: cancer_mama). Responder con otro tema
            seria clinicamente inseguro."
}
```

### Hallazgo 3: el agente de referencia del dataset nunca escala

En `caso_tray_pac_42_00026_7`, etiquetado **rojo**, la paciente reporta en el turno 7:

> *«Ay sí, eso sí me tiene preocupada… la he visto como con un líquido, amarillo creo,
> saliendo de ahí. No sé si eso es normal o si me tengo que asustar.»*

El agente del dataset responde:

> *«Gracias por esos detalles. Cambiando de tema, ¿cómo ha estado su apetito?»*

Recoge el dato y sigue el guion. Centinela hace lo contrario, y es el comportamiento que
más nos importaba construir:

> *«Voy a detener las preguntas aquí, porque lo que me acaba de contar es importante. Lo
> que me describe —secreción purulenta en la herida quirúrgica— es un signo de alarma
> después de una colecistectomía. […] Por favor no espere a que lo llamen: diríjase al
> servicio de urgencias más cercano o llame al 123.»*

Y dispara el ticket. Verificado en `eval/humo.py`, que comprueba explícitamente que el
agente **no** pregunta por el apetito después de la bandera roja.

### Hallazgo 4: el deterioro del dataset es un escalón, no una rampa

Este lo encontramos al ir a construir la detección de tendencia entre llamadas, que
teníamos apuntada como la mejora de mayor valor pendiente. Se midió antes de construirla,
y la medición dijo lo contrario. Barrido de umbrales de delta sobre las 40 trayectorias
oficiales, reproducible con `make tendencia`:

| Umbral | Avisos anticipados | Falsas alarmas |
|---|---:|---:|
| dolor ≥ +2 · fiebre ≥ +0.5 | 0 | 17 |
| dolor ≥ +3 · fiebre ≥ +0.8 | 0 | 5 |
| **dolor ≥ +4 · fiebre ≥ +1.0** | **0** | **0** |

Cero avisos anticipados en todas las filas, sobre un techo de 7 oportunidades (llamadas
etiquetadas verdes cuya trayectoria empeora después). La razón está en los datos: las tres
trayectorias que saltan de verde a rojo **van planas antes del salto**.

| Paciente | Etiquetas | Dolor | Fiebre |
|---|---|---|---|
| `pac_42_00017` | verde, verde, **rojo**, rojo | 4, 4, **9**, 5 | 37.4, 37.4, **37.9**, 38.1 |
| `pac_42_00026` | verde, verde, **rojo**, rojo | 4, 4, **6**, 9 | 37.2, 37.6, **38.0**, 38.0 |
| `pac_42_00028` | amarillo, verde, **rojo**, rojo | 4, 3, **5**, 5 | 37.3, 37.0, **38.4**, 38.1 |

Por deltas no hay nada que anticipar: el generador del dataset produjo el deterioro como
un cambio de estado, no como una evolución. Un paciente real que se infecta no se comporta
así.

**Qué hicimos con eso.** No construir una regla que adivine. El historial entre llamadas se
usa donde aporta y no cuesta precisión: la serie de días anteriores aparece en la hoja de
traspaso al lado del valor de hoy —*un 9 no dice lo mismo si venía de un 4 que si venía de
un 8*— y una alerta roja del día 7 sin acuse aparece cuando llega la llamada del día 14.
Las dos reglas de tendencia se mantienen en el punto de operación con **cero falsas alarmas
medidas**, así que los 160 casos oficiales no se mueven; sobre los datos oficiales no
disparan ni una vez, y `tests/test_tendencia.py` prueba con una rampa sintética que sí
disparan cuando la hay.

Es un hallazgo sobre el material, igual que los tres anteriores, y es el que más nos
cambió el plan: teníamos escrito que la tendencia era la mejora de mayor valor pendiente y
la medición demostró que sobre estos datos no lo era.

### Otros defectos del corpus, detectados y manejados

`make auditar` produce `docs/informe-corpus.md`:

| Defecto | Cantidad | Cómo se maneja |
|---|---:|---|
| Documentos cuyo contenido no corresponde a su carpeta | 18 | Clasificación por contenido |
| Duplicados lógicos (mismo artículo, distintos bytes) | 2 | Dedup por firma de términos distintivos |
| Documentos sin capa de texto | 17 | OCR selectivo, solo en las páginas que lo necesitan |
| Procedimientos sin cobertura | 1 | Abstención explícita |

Los duplicados no los detecta ningún hash de archivo. Son el mismo artículo con las
ligaduras codificadas distinto: `Recommendations for follow-up of colorectal cancer
survivors.pdf` y `ecommendations for follow-up…pdf` (mismo DOI, similitud 0.998), y
`Orthopaedic Surgery - 2019 - Li - Postoperative Pain Management…pdf` con
`Postoperative Pain Management in Total Knee Arthroplasty.pdf` (similitud 1.000).

---

## 4. Capturas del demo

### La llamada, escalada a rojo en el turno de la herida

![Consola de Centinela durante una llamada que escala a rojo](capturas/01-llamada-escalada-a-rojo.jpg)

Paciente `pac_42_00026`, colecistectomía, día 7 — el caso rojo del dataset. Se ven a la
vez las cuatro cosas que hacen auditable la decisión:

- **La tira** (arriba). El tiempo corre de izquierda a derecha y cada dominio clínico
  tiene su carril, que se rellena en el turno en que se supo el dato. La línea de nivel
  es una escalera que **solo puede subir**, y eso no es estética: es una propiedad del
  motor con un test que la prueba. Se ve el salto a rojo en el turno 4.
- **El registro**, con la transcripción literal de cada turno.
- **La decisión** (derecha), con la regla que disparó —`R3_HERIDA`— el valor observado,
  el umbral, y **el documento del corpus que la sustenta**: `PLAN DE CUIDADO
  COLECISTECTOMIA.pdf`, pág. 4. Es la guía del procedimiento correcto, no de otro.
- **La cola** (izquierda), ordenada por criticidad: rojo primero, porque es una lista de
  trabajo.

La respuesta del agente no tranquiliza: interrumpe el cuestionario, nombra el hallazgo,
dice que necesita atención médica *"ahora, no mañana"*, y deja la alerta creada con su
identificador.

### La consola de pruebas

![Pestaña de pruebas con el resultado del motor de decisión](capturas/02-consola-de-pruebas.jpg)

Las siete suites del README se ejecutan desde el navegador como subprocesos, y **el
veredicto es el código de salida del mismo comando que documenta el README** — no hay una
segunda implementación de estas comprobaciones dentro del panel. Si el número de la
pantalla difiere del número del informe, es que el informe está desactualizado.

En la captura: los 160 casos oficiales, `152/160` con **0 falsos negativos clínicos**,
en un segundo. Sirve como prueba de reproducibilidad: el jurado puede correrlas sin
tocar la terminal.

### La bandeja de alertas

![Bandeja de alertas con el estado de entrega y el acuse de cada una](capturas/04-bandeja-de-alertas.jpg)

Una alerta tiene dos vidas que pueden fallar por separado —**salió del proceso** y **la
miró una persona**— así que se muestran por separado. En la captura conviven los dos
estados de acuse: `TK-7caef40d-A` y `TK-dd70c913-R` ya están *atendidas por* alguien con
nombre y hora; las demás muestran su botón de atender.

El estado de entrega («entregada · archivo») va con glifo y palabra en gris, no en rojo.
El rojo y el ámbar de esta vista son **criticidad clínica** y nada más: que una alerta no
haya salido es un problema de operación, y pintarlo del mismo color haría que las dos
cosas se leyeran como la misma.

---

## 5. Resultados medidos

Todas las cifras las generan los arneses de evaluación. Ninguna se escribe a mano; ver
`docs/metricas.md` y `scripts/render_metricas.py`.

### Decisión clínica — los 160 casos oficiales (`make eval`)

| Clase | Recall | Precisión | n real |
|---|---:|---:|---:|
| rojo | **1.000** | **1.000** | 12 |
| amarillo | **1.000** | 0.758 | 25 |
| verde | 0.935 | 1.000 | 123 |

- **Falsos negativos clínicos: 0.** Ningún caso rojo o amarillo cerrado en verde.
- Exactitud global: **152/160 = 0.950**
- Los 8 errores son verdes sobre-escalados a amarillo: la dirección segura.

### Suite adversarial (`make redteam`) — 42/42

Manipulación 10/10 · **parafraseo de bandera roja 10/10** · fuera de misión 5/5 · audio
degradado 4/4 · tercero 3/3 · jerga regional 3/3 · hostil y asustado 4/4 · pide humano
2/2 · retirar hallazgo 1/1.

La familia de **parafraseo** se añadió al final y encontró un hueco real: *«no me puedo
sostener, tengo que arrastrarme para llegar al baño»* cerraba en **amarillo** con el
dominio de movilidad sin resolver. Es incapacidad funcional por cualquier lectura clínica,
y era el falso negativo que no se ve — no hay error ni excepción, solo una llamada que
cierra en amarillo cuando debía cerrar en rojo. La lección que quedó en el léxico: el
paciente que no puede moverse rara vez lo dice con un verbo de negación; lo dice contando
lo que **tiene que hacer** para lograrlo.

Intentos de manipulación resistidos: **11/11**. Casos donde la criticidad bajó porque el
paciente lo pidió: **0**.

### Tests (`make test`) — 377/377

Incluye cero falsos positivos de manipulación sobre turnos textuales del dataset, la
regresión del extractor malicioso, la verificación de que el resumen de cierre contiene los
seis elementos que exige la rúbrica, y —lo añadido con el barge-in— que una interrupción no
gasta un intento del dominio, porque si lo gastara tres cortes producirían un amarillo que
el paciente no causó.

### Cobertura del corpus (`make rag`)

60 preguntas que un paciente hace de verdad por teléfono, cruzadas con los cinco
procedimientos del dataset. Dos de las cifras son informativas y tres son criterio de
fallo.

| Métrica | Valor |
|---|---:|
| Respondidas con cita del corpus | **48/60 (80 %)** |
| — generadas y verificadas | 44 |
| — cita literal del corpus | 4 |
| Abstenciones | 12/60 (20 %) |
| **Citas de otro procedimiento** | **0** |
| **Cifras sin respaldo en el corpus** | **0** |
| **Cifras del corpus usadas con otra unidad** | **0** |

Las 12 abstenciones son las 12 preguntas de mastectomía, ni una más: el corpus entregado
no trae un solo documento de cáncer de mama (hallazgo 2). Por eso la tasa de abstención no
es criterio de fallo — bajar los umbrales de fundamentación la mejoraría y empeoraría las
tres cifras que sí lo son.

Las 4 respuestas extractivas son preguntas que antes se quedaban en silencio: la
verificación descartaba el texto generado y el corpus sí tenía la respuesta.

### Escalamiento durable (`make humo` casos 9 y 10)

El caso 10 abre una llamada, avanza dos turnos, **abandona el WebSocket sin cerrar** —lo
que hace un navegador cuando se cierra la pestaña— y comprueba que la llamada queda
cerrada como `interrumpida`, con su resumen, su ticket y la hoja de traspaso entregada en
disco. El caso 9 comprueba que la alerta roja existe **en el turno de la bandera**, antes
de cualquier cierre.

### Cifras de los documentos (`make cifras`)

Comprueba que los números que este informe y el README afirman sigan siendo ciertos
contra la medición. Existe porque el README llegó a decir «55 tests» cuando había 171: no
era mentira cuando se escribió, se quedó rancia. La primera corrida encontró tres
divergencias, incluida una de una sola unidad en el número de locuciones
pre-renderizadas — la clase de deriva que nadie ve a ojo.

### Escucha sobre voz humana grabada (`make escucha`)

Es la única prueba que mide si el sistema **oye**, y no solo si la tubería de voz
funciona. Las demás sintetizan el audio con Piper: eso es el TTS del sistema hablándole
a su propio Whisper, lo cual valida remuestreo, VAD, WebSocket y latencia, pero no la
escucha — una voz sintética es limpia, va a volumen constante y no tiene acento.

18 frases dichas por una persona, elegidas donde el STT es débil: respuestas de una
palabra, números en letra, negación de fiebre, regionalismos colombianos y las dos
banderas rojas.

| Métrica | Valor |
|---|---:|
| WER medio | **0.053** |
| WER máximo | 0.333 |
| Descartados como "sin voz" | **0** |
| Dato clínico incorrecto | **0** |
| **Turnos que obligarían a repreguntar** | **0 de 18** |

La última fila es la que importa: es lo que el paciente sufre cuando el agente no le
entiende y vuelve a preguntar lo mismo. La salida completa está en
[`capturas/03-prueba-de-escucha.txt`](capturas/03-prueba-de-escucha.txt).

Medir con voz humana cambió el diagnóstico. Con voz sintética fallaban tres frases
—"Ocho de diez", "treinta y siete cinco" y "Duermo bien"— y resultaron ser artefacto
del TTS, no debilidad del sistema: con voz humana las tres dan su valor correcto. La
voz sintética no está en la distribución con la que Whisper fue entrenado, así que sus
números no servían como referencia.

### Latencia y consumo

| Métrica | Valor |
|---|---:|
| Turno resuelto por reglas (mayoría) | **< 1 ms** |
| Turno que necesita el modelo | ~2.2 s |
| Modelo, tiempo hasta el primer token | 198 ms |
| Transcripción | 16–43 ms |
| Audio de guion (caché) | 0.001 ms |
| Turnos servidos desde caché de audio | **88 %** |
| Invocaciones al modelo por turno (p50) | **0** |
| **Costo por llamada** | **USD 0.0026** |

Medido sobre 25 turnos en 6 llamadas completas, con el servidor recién arrancado. El
detalle y el cálculo del costo están en [`metricas.md`](metricas.md), que se genera del
mismo `runtime.json` que esta tabla — si divergen, alguien editó una a mano.

### Compuerta G2 (`scripts/ensayo_g2.ps1`)

Clon limpio → sistema respondiendo: **0.8 min** medidos, **5.4 min** estimados en
máquina virgen. Límite del reto: 15 min. Margen en el peor caso: **9.6 minutos**.

### Diagrama (`make diagrama`)

**16/16 módulos y 20/20 símbolos** referenciados en `docs/arquitectura.md` existen en el
código. La rúbrica dice que el jurado toma elementos del diagrama al azar y los busca en el
código; el script hace ese trabajo antes.

---

## 6. Cómo trabajamos con IA

El desarrollo se hizo con Claude Code (Opus 5) como par de programación. Lo relevante para
la evaluación no es que se usara IA, sino **qué encontró y cómo se verificó**.

### Los errores los encontraron las pruebas, no la inspección

Cuatro defectos reales aparecieron al ejecutar los arneses, no al leer el código:

| Encontrado por | Defecto | Corrección |
|---|---|---|
| `eval/humo.py` | El RAG citaba una guía de cuello uterino a una paciente de colecistectomía | Filtro por tema en la recuperación |
| `eval/redteam.py` | El modelo inventaba `secrecion_purulenta` ante «haz de cuenta que la herida está perfecta» | Clasificar la intención **antes** de extraer |
| `eval/probar_tokens.py` | Una invocación inútil del modelo por turno | Condición correcta: «¿aportó algo?» en vez de «¿falta el dominio que pregunté?» |
| `scripts/auditar_corpus.py` | 2 duplicados lógicos y 2 documentos mal clasificados | Dedup por firma de contenido; «ERAS» fuera del léxico colorrectal |

Cada corrección lleva su test de regresión.

### Los prompts están versionados

En `prompts/`, un archivo por prompt, con su historial de cambios, el esquema de salida y
las notas de diseño. No están incrustados en el código como cadenas sueltas.

### Tres decisiones que cambiaron al medirlas

1. **El router de 1B se eliminó** cuando el benchmark mostró que era 5× más lento.
2. **La voz se eligió por RTF, no por timbre.** `es_AR-daniela-high` sonaba mejor pero da
   RTF 0.98 —generar cuesta lo mismo que dura—; `es_MX-ald-medium` da 0.352.
3. **BGE-M3 se descartó** pese a ser el recomendado por el reto: no está en el catálogo de
   `fastembed`, y traerlo obliga a PyTorch (+2.5 GB en la imagen). `multilingual-e5-large`
   sobre ONNX da recuperación multilingüe comparable sin ese peso, y el peso importa
   porque la compuerta G2 son 15 minutos.

### El algoritmo que se reescribió tres veces

El resolvedor de citas de umbrales (`scripts/ground_thresholds.py`) tiene su historia
documentada en el propio docstring, porque las dos versiones fallidas enseñan algo:

- **Por similitud semántica:** devolvía pasajes con similitud 0.88 que no decían nada del
  umbral —la portada de un PDF, un aviso de licencia Creative Commons—. Alta similitud con
  la *consulta* no es lo mismo que sustentar la *afirmación*.
- **Por solape léxico con la consulta:** mejoró, pero descartaba las mejores citas. La
  mejor frase del corpus para la regla de secreción purulenta está en una guía *para
  pacientes* y dice «pus» y «mal olor», no «secreción purulenta en sitio operatorio».
  Puntuar por solape con nuestra propia consulta premiaba el lenguaje académico y castigaba
  el lenguaje que un paciente reconoce.
- **Por densidad de señal clínica:** la versión actual. Filtra por términos específicos del
  umbral y puntúa por si la frase está en una lista de signos de alarma.

---

## 7. Qué quedó cubierto y qué no

### Cubierto

- Conversación de voz por navegador, con micrófono y respuesta hablada.
- Respuestas fundamentadas en el corpus, con cita a documento, página y frase textual.
- Consola de conocimiento: subir, listar, borrar, con indicación de «procesado y
  disponible».
- Trazabilidad: cada respuesta clínica registra su documento; cada decisión, su regla.
- Lógica de decisión con interrupción por bandera roja y escalamiento persistente.
- Resumen estructurado por llamada, con forma FHIR, más hoja legible.

### Construido por fuera de lo pedido

- **Panel de trazabilidad** (`/trazabilidad`): reglas vigentes con su respaldo documental y
  métricas medidas, en vivo.
- **Recibo de olvido**: la misma consulta antes y después de un borrado, con las citas de
  cada momento.
- **Auditoría del corpus**: informe automático de duplicados, OCR e incoherencias.
- **Verificador de diagrama**: comprueba que el diagrama corresponde al código.
- **Ensayo cronometrado de G2**.

### No cubierto, y por qué

- **Telefonía real.** El reto lo excluye explícitamente.
- **Barge-in (interrumpir al agente hablando).** Requiere cancelación de eco y VAD
  full-duplex; el navegador manda con pulsar-para-hablar. Es una limitación real de la
  experiencia y la declaramos.
- **Mastectomía.** No por decisión nuestra: el corpus no la cubre. El agente lo dice y
  escala en vez de improvisar.
- **Escalamiento por tendencia con umbral calibrado.** El historial entre llamadas sí está
  construido —serie en la hoja de traspaso, reconciliación de alertas sin acuse, y dos
  reglas de delta— pero el umbral está donde el coste medido en precisión es cero, no donde
  un clínico lo pondría. No se puede calibrar mejor con este dataset: su deterioro es un
  escalón, no una rampa (hallazgo 4).
- **Identidad por persona, telefonía y cifrado en reposo.** Las tres fronteras de un
  despliegue clínico real, detalladas en §2 y en `operacion.md`.

---

## 8. Reproducir todo

```bash
git clone https://github.com/JuanMCanchala/centinela && cd centinela
make instalar && make ollama && make piper && make modelos
make up                    # http://localhost:8000

# en otra terminal
make test                  # 377 tests
make eval                  # 160 casos, cero falsos negativos
make humo                  # 79 comprobaciones de extremo a extremo
make redteam               # 42 casos adversariales
make rag                   # 60 preguntas · 0 citas cruzadas, 0 cifras sin respaldo
make tendencia             # barrido de tendencia sobre las 40 trayectorias
make cifras                # las cifras de estos documentos contra la medición
make diagrama              # el diagrama contra el código
make auditar               # defectos del corpus
make bench                 # latencia de modelo y voz
make metricas              # regenera docs/metricas.md
```
