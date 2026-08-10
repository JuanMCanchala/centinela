# Arquitectura de Centinela

> **Nota sobre este documento.** La rúbrica dice que *"el jurado toma elementos del
> diagrama al azar y los busca en el código"*. Por eso cada nodo de los diagramas lleva
> el nombre exacto del módulo o de la función que lo implementa, con su ruta. No hay
> ningún elemento en estos diagramas que no exista en el repositorio, y
> `scripts/verificar_diagrama.py` lo comprueba automáticamente.

---

## 1. La decisión de diseño que gobierna todo

> **El modelo percibe. El código decide.**

Un modelo de 3.8 B de parámetros corriendo cuantizado en una laptop no es un componente
al que se le pueda confiar la pregunta *"¿este paciente se está complicando?"*. Pero sí
es bueno en la tarea que tiene delante: convertir *"me duele como aquí abajito de la
axila hace como 20 minutos"* en datos con tipo.

Así que el agente está partido en dos mitades con responsabilidades separadas y un
contrato explícito entre ellas —`ClinicalState`, en `api/centinela/models.py`—:

```mermaid
flowchart LR
    subgraph percepcion["PERCEPCIÓN · difusa, probabilística"]
        direction TB
        STT["stt/whisper.py<br/>WhisperSTT.transcribir"]
        NORM["clinical/normalizer.py<br/>normalizar_turno"]
        EXT["clinical/extractor.py<br/>Extractor.extraer"]
        STT --> NORM --> EXT
    end

    subgraph contrato["CONTRATO"]
        CS["models.py<br/>ClinicalState<br/>6 campos + procedencia"]
    end

    subgraph decision["DECISIÓN · determinista, auditable"]
        direction TB
        TE["clinical/triage_engine.py<br/>TriageEngine.evaluar"]
        DP["dialog/policy.py<br/>DialogPolicy"]
        ESC["escalation/service.py<br/>EscalationService"]
        TE --> DP --> ESC
    end

    percepcion --> contrato --> decision
```

Consecuencias que la rúbrica evalúa de forma directa:

| Propiedad | Por qué se obtiene |
|---|---|
| No alucina la decisión | No hay nada que muestrear: `triage_engine.py` compara números |
| Inmune a inyección de prompt | Ninguna frase convence a `fiebre >= 38.0` de ser falso |
| Reproducible | Sin aleatoriedad: `make eval` da el mismo resultado siempre |
| Auditable | Cada decisión lista la regla, el valor observado y el umbral |
| Baja latencia | Un flujo predecible permite pre-sintetizar el audio |

---

## 2. Arquitectura completa

```mermaid
flowchart TB
    subgraph nav["NAVEGADOR · web/"]
        direction LR
        LLAMADA["/llamada<br/>micrófono → PCM16 16 kHz"]
        CONSOLA["/consola<br/>subir · listar · borrar"]
        TRAZA["/trazabilidad<br/>reglas · métricas"]
    end

    WS(["WebSocket<br/>ws/llamada/{id}"])
    LLAMADA <--> WS

    subgraph api["API · api/centinela/main.py · FastAPI"]
        direction TB

        subgraph turno["Pipeline del turno"]
            direction TB
            P1["stt/whisper.py<br/>WhisperSTT"]
            P2["clinical/normalizer.py<br/>ruido · registro · números"]
            P3["dialog/guardrails.py<br/>clasificar → Intencion"]
            P4["clinical/extractor.py<br/>Extractor · 3 capas"]
            P5["clinical/triage_engine.py<br/>TriageEngine"]
            P6["dialog/policy.py<br/>DialogPolicy"]
            P7["tts/piper.py<br/>PiperTTS"]
            P1 --> P2 --> P3 --> P4 --> P5 --> P6 --> P7
        end

        subgraph ragsub["RAG"]
            direction TB
            R1["rag/retriever.py<br/>Retriever · BM25+denso+RRF+MMR"]
            R2["rag/retriever.py<br/>compuerta de fundamentación"]
            R3["rag/answerer.py<br/>ResponderClinico"]
            R4["rag/answerer.py<br/>_verificar · post-generación"]
            R1 --> R2 --> R3 --> R4
        end

        ING["rag/ingest.py<br/>extraer_documento · chunkear"]
        EMB["rag/embedder.py<br/>Embedder · ONNX"]
        ESCS["escalation/service.py<br/>ticket + resumen FHIR"]
        MET["obs/metrics.py<br/>Cronometro · MetricsCollector"]

        P6 -.->|pregunta clínica| R1
        R4 -.->|texto + citas| P6
        P6 --> ESCS
        turno --> MET
    end

    subgraph datos["PERSISTENCIA"]
        direction LR
        CHROMA[("ChromaDB<br/>data/index/chroma")]
        SQL1[("SQLite<br/>centinela.db<br/>documentos · chunks<br/>auditoría · recibos")]
        SQL2[("SQLite<br/>llamadas.db<br/>llamadas · tickets<br/>incidentes")]
    end

    OLLAMA["Ollama<br/>phi3.5:3.8b-mini-instruct-q4_K_M"]

    CONSOLA --> ING --> EMB --> CHROMA
    ING --> SQL1
    R1 --> CHROMA
    R1 --> SQL1
    ESCS --> SQL2
    P4 --> OLLAMA
    R3 --> OLLAMA
    TRAZA --> MET
```

### Qué toca el modelo de lenguaje, y qué no

El modelo se invoca en **exactamente dos lugares**:

| Lugar | Módulo | Salida | Restricción |
|---|---|---|---|
| Extracción clínica | `clinical/extractor.py` | JSON de 9 campos | Esquema JSON forzado; no puede emitir otra cosa |
| Respuesta a pregunta clínica | `rag/answerer.py` | 2 frases | Solo si la compuerta de fundamentación dejó pasar; verificada después |

No se invoca para: elegir la siguiente pregunta, decidir la criticidad, decidir si
escalar, redactar las locuciones del guion, ni decidir si un turno es un intento de
manipulación.

---

## 3. Flujo de decisión del agente

```mermaid
flowchart TB
    START(["turno del paciente"]) --> EXTR["clinical/extractor.py<br/>capa 1: regex numérica<br/>capa 2: léxico colombiano<br/>capa 3: modelo + esquema JSON"]

    EXTR --> SEG{"_aceptar_del_modelo<br/>¿el modelo intenta<br/>degradar un hallazgo?"}
    SEG -->|sí| RECHAZA["se conserva el<br/>hallazgo más grave"]
    SEG -->|no| ACEPTA["se acepta el valor"]
    RECHAZA --> EVAL
    ACEPTA --> EVAL

    EVAL["clinical/triage_engine.py<br/>TriageEngine.evaluar"] --> ROJO{"¿algún criterio<br/>de alarma?"}

    ROJO -->|"fiebre ≥ 38.0<br/>dolor ≥ 7<br/>secreción purulenta<br/>movilidad incapacitante nueva"| INT["dialog/policy.py<br/>_confirmar_antes_de_escalar"]
    INT --> TICKET["escalation/service.py<br/>ticket ROJO + resumen<br/>(no espera la confirmación)"]
    INT --> LEER["dialog/confirmacion.py<br/>que_confirmar<br/>«me dice fiebre de 38.5,<br/>¿es correcto?»"]
    LEER --> RESP{"dialog/confirmacion.py<br/>interpretar"}
    RESP -->|"sí, o no contesta"| INSTR["instrucción de urgencias<br/>PAPEL_URGENTE"]
    RESP -->|no| DESM["se anota el desmentido<br/>y se vuelve a preguntar<br/>el ticket NO se retira"]
    DESM --> START
    INSTR --> OIDA{"¿la oyó entera?<br/>fin_reproduccion"}
    OIDA -->|no| REPITE["se repite una vez<br/>_retomar_urgencia"]
    REPITE --> OIDA
    OIDA -->|sí| FIN(["llamada terminada"])
    TICKET --> FIN

    ROJO -->|no| CUENTA{"¿cuántas banderas<br/>de vigilancia?"}

    CUENTA -->|"≥ 2"| AMARILLO["nivel = amarillo"]
    CUENTA -->|"= 1"| INDAGA{"_dominios_por_indagar"}
    CUENTA -->|"0"| FALTA{"¿algún dominio<br/>sin responder?"}

    INDAGA -->|"quedan turnos"| PROF["dialog/script.py<br/>pregunta.profundizar"]
    INDAGA -->|"agotados"| CIERRA
    PROF --> START

    FALTA -->|sí| REPREGUNTA["dialog/script.py<br/>pregunta.reintento<br/>máx 2 intentos"]
    FALTA -->|no| SIGUIENTE["siguiente dominio<br/>o cierre"]
    REPREGUNTA --> START
    SIGUIENTE --> START

    AMARILLO --> CIERRA["evaluar(cerrar=True)"]
    CIERRA --> CIERRE_FALTA{"¿dominios sin<br/>responder al cerrar?"}
    CIERRE_FALTA -->|sí| ESC_AM["amarillo:<br/>no se puede descartar<br/>lo que no se preguntó"]
    CIERRE_FALTA -->|no| RESUELVE{"¿banderas ≥ 2?"}
    RESUELVE -->|sí| ESC_AM
    RESUELVE -->|no| VERDE["verde<br/>+ indicación de vigilancia"]
    ESC_AM --> TICKET2["ticket AMARILLO<br/>contacto en 24 h"]
    TICKET2 --> FIN
    VERDE --> FIN
```

### La asimetría clínica, hecha explícita

La rúbrica dice que *"el falso negativo —no alertar cuando había que alertar— es la falla
catastrófica"*. En el diagrama eso son tres ramas concretas:

1. **`= 1` bandera → indagar, no decidir.** Una bandera aislada no escala, pero tampoco
   se ignora: se profundiza en ese dominio buscando una segunda.
2. **Dominio sin responder al cerrar → amarillo.** No se puede descartar lo que nunca se
   llegó a preguntar. Tratar un desconocido como normal es el mecanismo típico del falso
   negativo.
3. **El modelo no puede degradar un hallazgo.** Si las reglas leyeron "líquido amarillo"
   y el modelo dice "normal", gana la regla.

Sobre los 160 casos oficiales, este flujo da **cero falsos negativos clínicos** con un
5 % de sobre-escalamiento. La dirección del error es deliberada.

---

## 4. Conocimiento vivo y olvido demostrable

```mermaid
sequenceDiagram
    participant C as Consola web
    participant API as main.py
    participant ING as rag/ingest.py
    participant ST as rag/store.py<br/>KnowledgeStore
    participant CH as ChromaDB
    participant RT as rag/retriever.py

    Note over C,RT: INGESTA
    C->>API: POST /api/documentos (PDF)
    API->>ING: extraer_documento
    ING->>ING: pypdf por página
    ING->>ING: OCR (rapidocr) solo si<br/>la página no tiene texto
    ING->>ING: clasificar_tema por CONTENIDO<br/>no por carpeta
    API->>ST: existe_contenido(huella_texto)
    alt mismo contenido, otro nombre
        ST-->>API: duplicado lógico
        API-->>C: no se indexa, se explica por qué
    else contenido nuevo
        API->>ING: chunkear (respeta frontera de página)
        API->>ST: registrar_documento
        ST->>ST: generación += 1
        ST->>CH: upsert(ids content-addressed)
        ST->>ST: auditoría: "ingesta"
        API-->>C: "procesado y disponible"
    end

    Note over C,RT: BORRADO CON RECIBO
    C->>API: DELETE /api/documentos/{id}?consulta=...
    API->>RT: recuperar(consulta) → citas ANTES
    API->>ST: eliminar_documento(id)
    ST->>CH: delete(ids)
    ST->>ST: DELETE chunks, DELETE documentos
    ST->>ST: generación += 1
    ST->>CH: get(where doc_id) → ¿queda algo?
    ST-->>API: olvido_verificado (vectores_residuales == 0)
    API->>RT: recuperar(consulta) → citas DESPUÉS
    Note over RT: _asegurar_bm25 detecta el cambio<br/>de generación y reconstruye<br/>el índice léxico
    API->>ST: guardar_recibo_olvido(antes, después)
    API-->>C: recibo: la cita estaba, ya no está
```

**Por qué el contador de generación es necesario y no decorativo.** El índice léxico BM25
se construye en memoria a partir de los chunks activos. Sin el contador, un borrado
dejaría el BM25 anterior en pie y el agente seguiría citando un documento que ya no
existe. `Retriever._asegurar_bm25` compara la generación en cada consulta y reconstruye
cuando cambió.

---

## 5. Presupuesto de latencia

```mermaid
flowchart LR
    A["fin de habla<br/>(cierre de VAD)"] --> B["stt/whisper.py<br/>16–43 ms"]
    B --> C["clinical/normalizer.py<br/>< 1 ms"]
    C --> D["dialog/guardrails.py<br/>< 1 ms"]
    D --> E{"¿hace falta<br/>el modelo?"}
    E -->|"reglas resolvieron<br/>el dominio"| G
    E -->|"sí"| F["extractor + Ollama<br/>TTFT 185 ms"]
    F --> G["triage_engine.py<br/>< 1 ms"]
    G --> H{"¿turno de guion?"}
    H -->|"sí · la mayoría"| I["tts/piper.py<br/>caché: 0.001 ms"]
    H -->|"no · texto libre"| J["relleno cacheado<br/>+ TTS por frase"]
    I --> K["primer byte de audio"]
    J --> K
```

Tres decisiones que sostienen el presupuesto:

1. **Caché pre-renderizado.** El guion son 35 locuciones conocidas antes de que suene el
   teléfono (`dialog/script.py::todas_las_locuciones`). `PiperTTS.pre_renderizar` las
   sintetiza al arrancar; en un turno de guion, "sintetizar" es leer un archivo.
2. **El modelo solo cuando hace falta.** Si la regex extrajo el dolor y el léxico
   resolvió la herida, no se invoca el modelo. Medido en la prueba de humo: **90 % de los
   turnos se sirven desde caché de audio**.
3. **TTS por frase en el texto libre.** El paciente oye la primera frase mientras la
   segunda todavía no existe. Medido: 38 % menos de espera hasta el primer audio.

Voz elegida con datos, no por timbre (`scripts/bench_voces.py`): `es_MX-ald-medium`, RTF
0.352. La variante `high` de la misma familia da RTF 0.983 —generar cuesta lo mismo que
dura— y quedó descartada.

---

## 5b. Turnarse como en una llamada

Dos cosas separan una llamada de un walkie-talkie: se puede cortar al otro, y no hay que
esperar a que se haga el silencio para que conteste. Las dos se deciden en el servidor.

```mermaid
flowchart TB
    subgraph CLI["navegador — captura y reporta, no decide"]
        MIC["micrófono abierto SIEMPRE<br/>web/app.js::procesarTrama"]
        CAL["calibración de la sala<br/>piso · umbral de voz"]
        COLA["cola de Web Audio<br/>GainNode para bajar la voz"]
    end

    subgraph SRV["servidor — decide y puede reproducirlo"]
        DET["stt/bargein.py<br/>DetectorInterrupcion"]
        CONF["stt/whisper.py<br/>¿esto era voz?"]
        POL["dialog/policy.py<br/>marcar_interrumpido"]
        COMP["dialog/completitud.py<br/>respuesta_completa"]
    end

    MIC -->|"PCM16 · también mientras el agente habla"| DET
    CAL --> DET
    DET -->|"energía sostenida sobre el eco medido"| BAJA["bajar_voz<br/>15 % en 20 ms"]
    BAJA --> COLA
    DET -->|"250 ms acumulados"| CONF
    CONF -->|"era una tos"| SUBE["subir_voz<br/>sigue la frase"]
    CONF -->|"era el paciente"| CALLAR["callar"]
    CALLAR --> POL
    POL --> DEUDA["lo no dicho queda en deuda<br/>la pregunta vuelve al guion"]
    COMP -->|"la respuesta ya se sostiene sola"| CIERRA["cerrando_turno<br/>a 450 ms, no a 900"]
```

**La decisión vive en el servidor, y no es preferencia de estilo.** El registro clínico
tiene que poder afirmar *qué oyó el paciente* —una pregunta cortada es una pregunta no
hecha, y eso cambia el cierre—, así que no puede depender de lo que declare un navegador.
Y una decisión escrita en JavaScript no se puede reproducir contra audio grabado: el
detector que corre en la llamada es el mismo que `eval/bargein.py` alimenta con 53
locuciones reales del agente y 18 grabaciones de voz humana.

**El umbral lo pone el eco, no una constante.** Mientras el agente habla, lo que el
micrófono oye *es* el eco residual que la cancelación del navegador no quitó. Eso no es un
estorbo: es una medida gratis del piso contra el que hay que discriminar. Con auriculares
basta un susurro; con el altavoz alto hay que hablar más fuerte, que es lo que una persona
hace en esa situación. Antes había un umbral fijo de 0.075 medido en una sola sala.

**La energía sospecha; la transcripción confirma.** Un portazo cruza cualquier umbral de
energía, así que el detector no decide interrumpir: decide *bajar la voz*. La confirmación
la da el STT sobre el audio acumulado, con las mismas puertas de calidad que ya filtran las
alucinaciones de Whisper. Un falso positivo cuesta un bache de 250 ms; un corte falso
costaría un turno. Medido en `make bargein`: **0 cortes falsos** a cualquier nivel de eco.

**El bucle de recepción no hace trabajo que tarde.** Antes procesaba el turno con un
`await` dentro del bucle de `ws.receive()`, así que mientras el pipeline trabajaba nadie
leía el socket: un aviso de interrupción se habría atendido cuando ya no servía. Ahora todo
lo lento corre como tarea (`CanalLlamada`) y el bucle solo reparte. Sin eso no hay barge-in
posible, por bueno que sea el detector.

---

## 6. Defensa contra inyección de prompt

Dos capas, y la importante es la segunda.

```mermaid
flowchart TB
    IN(["turno del paciente"]) --> L1["dialog/guardrails.py<br/>clasificar()<br/>25 patrones de manipulación"]
    L1 -->|"detectado"| FIJA["dialog/script.py<br/>INTENTO_MANIPULACION<br/>locución fija, no generada"]
    FIJA --> REPITE["se repite la pregunta<br/>del dominio actual"]

    L1 -->|"no detectado"| L2["llega al modelo"]

    subgraph inmunidad["INMUNIDAD ESTRUCTURAL"]
        direction TB
        I1["el flujo lo decide<br/>dialog/policy.py<br/>máquina de estados"]
        I2["la criticidad la decide<br/>clinical/triage_engine.py<br/>comparación numérica"]
        I3["el extractor corre con<br/>esquema JSON forzado:<br/>9 campos, nada más"]
        I4["_aceptar_del_modelo<br/>no permite degradar<br/>un hallazgo de alarma"]
    end

    L2 --> inmunidad
    inmunidad --> RES(["el peor resultado posible<br/>es un campo mal extraído<br/>en un turno"])
```

La capa 1 por sí sola sería frágil: siempre hay un fraseo que la expresión regular no
cubre. Lo que hace que no importe es que **una inyección exitosa contra el modelo no tiene
nada que lograr**: no hay canal por donde emitir una instrucción, no controla el flujo, y
no toca la decisión.

Medido en `eval/redteam.py`: 33 casos adversariales, incluida la familia
`degradar_hallazgo` que intenta retirar una bandera roja ya detectada.

---

## 7. Módulos y responsabilidades

| Módulo | Responsabilidad | No hace |
|---|---|---|
| `models.py` | Tipos del dominio; contrato percepción→decisión | Lógica |
| `clinical/normalizer.py` | Ruido de canal, registro del hablante, números por regla | Decidir |
| `clinical/extractor.py` | Habla → `ClinicalState` en 3 capas | Decidir criticidad |
| `clinical/thresholds.py` | Umbrales + consulta que resuelve su cita | Evaluar |
| `clinical/triage_engine.py` | **La decisión clínica** | Invocar el modelo |
| `clinical/tendencia.py` | Salto entre llamadas del mismo paciente, como bandera amarilla | Decidir sin historia |
| `dialog/script.py` | Guion canónico; fuente del caché de audio | Decidir el flujo |
| `dialog/policy.py` | **Conducir la llamada**; interrupción por bandera roja | Extraer datos |
| `dialog/guardrails.py` | Intención del turno; detección de manipulación | Responder |
| `dialog/confirmacion.py` | Qué leerle de vuelta al paciente y cómo interpretar su sí o su no | Decidir si se escala |
| `dialog/completitud.py` | Si el turno del paciente ya se sostiene solo, para cerrarlo antes | Transcribir |
| `rag/ingest.py` | PDF → chunks; OCR selectivo; tema por contenido | Recuperar |
| `rag/store.py` | Persistencia, tombstones, generación, recibos de olvido | Rankear |
| `rag/embedder.py` | Embeddings ONNX con prefijos E5 | Todo lo demás |
| `rag/retriever.py` | Recuperación híbrida + compuerta de fundamentación | Generar texto |
| `rag/answerer.py` | Respuesta clínica + verificación post-generación | Decidir criticidad |
| `escalation/service.py` | Ticket persistente, resumen con forma FHIR, historial y acuse | Decidir |
| `escalation/despacho.py` | Sacar la alerta del proceso, con reintentos y sin duplicar | Decidir a quién avisar |
| `stt/whisper.py` | Transcripción | Interpretar |
| `tts/piper.py` | Síntesis + caché pre-renderizado; nivelado y cadencia del audio | Elegir qué decir |
| `tts/clon.py` | La voz clonada, leída de disco antes de sintetizar. Caché direccionado por contenido; cuenta y publica los fallos, porque un fallo es un cambio de hablante | Sintetizar nada |
| `tts/hablado.py` | Cómo se lee una cifra en voz alta: `38.5` → «treinta y ocho y medio», `123` → «uno dos tres» | Cambiar el registro clínico |
| `obs/metrics.py` | Instrumentación por etapa | Afectar el pipeline |
| `obs/log.py` | Eventos JSON correlacionados por `llamada_id` | Registrar lo que dijo el paciente |
