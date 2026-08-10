# Operación

Cómo se corre esto cuando ya no es una demo. Escrito para la persona que recibe una
llamada a las tres de la mañana diciendo que una alerta no salió.

---

## Configuración

Todo por variables de entorno, con valores por defecto que funcionan sin tocar nada.
Están en [`api/centinela/config.py`](../api/centinela/config.py) y se publican en
`GET /api/salud`.

| Variable | Por defecto | Qué hace |
|---|---|---|
| `CENTINELA_TIMEOUT_LLAMADA_S` | `180` | Segundos sin actividad tras los que una llamada se cierra sola |
| `CENTINELA_SLA_ROJO_MIN` | `15` | Minutos de plazo para acusar una alerta roja |
| `CENTINELA_SLA_AMARILLO_H` | `24` | Horas de plazo para acusar una alerta de vigilancia |
| `CENTINELA_WEBHOOK_ALERTAS` | *(vacío)* | URL a la que se entregan las alertas, además del disco |
| `CENTINELA_SECRETO_WEBHOOK` | *(vacío)* | Secreto para la firma HMAC-SHA256 del cuerpo |
| `CENTINELA_TOKEN` | *(vacío)* | Si se define, protege los endpoints que modifican algo y la llamada entera, canal de voz incluido. **Una sola palabra** (ver más abajo) |
| `CENTINELA_MAX_MB_DOC` | `64` | Tope de tamaño de un PDF subido |
| `CENTINELA_PUERTO` | `8000` | A qué puerto apuntan **los arneses** (`eval/destino.py`). `make PUERTO=8001` lo propaga solo |
| `CENTINELA_URL` | *(vacío)* | Para apuntar los arneses a otra máquina entera. Gana sobre el puerto |
| `CENTINELA_BARGEIN` | `1` | El paciente puede cortarle la palabra al agente. A `0`, la voz sale entera |
| `CENTINELA_BARGEIN_MARGEN_ECO` | `1.8` | Cuánto por encima del eco medido hay que hablar para cortar |
| `CENTINELA_BARGEIN_MS_CONF` | `250` | Cuánto audio se acumula antes de preguntarle al STT si eso era voz |
| `CENTINELA_CIERRE_ADAPTATIVO` | `1` | El turno cierra en cuanto la respuesta se sostiene sola. A `0`, siempre el techo |
| `CENTINELA_CIERRE_MIN_MS` | `450` | Plazo mínimo: por debajo no se cierra ni con la respuesta más clara |
| `CENTINELA_GRACIA_RECONEXION_S` | `20` | Cuánto se espera a que el navegador vuelva antes de dar la llamada por colgada. A `0`, cierre inmediato como antes |
| `CENTINELA_VOZ_CLONADA` | `1` | La voz del agente sale de `data/audio_clon/`. A `0` habla enteramente Piper: es el interruptor para descartar la voz clonada sin borrar nada |
| `CENTINELA_LLM_MODEL` | `phi3.5:3.8b-mini-instruct-q4_K_M` | Modelo declarado (compuerta G3) |
| `CENTINELA_DIR_RUNTIME` | `data/runtime` | Dónde vive la base de datos de llamadas |
| `CENTINELA_LLM_TIMEOUT_S` | `12` | Cuánto se le deja al modelo antes de darlo por perdido. Eran 60 s, que es un valor de script y no de conversación: un turno que espera un minuto ya no es un turno. El techo sale de la medición —la extracción tarda 2 255 ms en P50 y 2 924 ms en P95 sobre 366 invocaciones reales— más margen para el arranque en frío de Ollama |
| `CENTINELA_LLM_KEEP_ALIVE` | `30m` | Cuánto le pedimos a Ollama que mantenga el modelo en memoria. Sin esto usa su defecto de 5 min, y el efecto está medido: con el modelo caliente el primer turno cuesta **2 028 ms**, en frío **6 152 ms**. El servidor ya calienta al arrancar, pero ese calentamiento caducaba antes de que el jurado terminara de leer el README |
| `CENTINELA_TTS_VELOCIDAD` | `1.0` | `length_scale` de Piper. Queda en 1.0 porque el ruido de duración del modelo es del 3 % y un ajuste del 5 % no se distingue de él: medido, 1.0 da 5.117 s y 1.05 da 5.083 s sobre la misma frase. Con 1.15 o más sí se oye. `make muestras` genera el A/B |

**Si se cambia `CENTINELA_TTS_VELOCIDAD`, el caché de audio se regenera solo.** La velocidad
entra en la firma de `data/audio_cache/manifiesto.json`, así que al arrancar se vuelven a
sintetizar las 59 locuciones (~18 s) en vez de servir las viejas. Lo mismo pasa al editar el
texto de una locución. `pre_renderizado.renovadas_por_cambio_de_texto` en `/api/salud` dice
cuántas se rehicieron.

El `1.8` del margen de eco lo eligió el barrido de `make bargein-barrido`, y
`tests/test_bargein.py` comprueba que la configuración no se separe del módulo: tener el
número medido en un sitio y el que corre en otro es peor que no medirlo —pasó, y el test
existe por eso. Subirlo hace más difícil interrumpir y más raro el bache falso; bajarlo, lo
contrario.

**No hay variable de techo del turno.** El techo de 900 ms lo pone el VAD del navegador,
que es quien mide el silencio en el micrófono (`web/app.js`, `VAD.msSilencioParaCerrar`).
Publicar aquí un `CENTINELA_CIERRE_MAX_MS` sería ofrecer un mando desconectado.

**La ventana de gracia no debilita el cierre.** El cierre forzado por socket caído sigue
ocurriendo con el mismo motivo (`interrumpida`); lo único que cambia es que se retrasa hasta
que la ventana expira, para que un bache de red de dos segundos no cueste la llamada. El
barredor de inactividad (`CENTINELA_TIMEOUT_LLAMADA_S`) sigue siendo la red de último
recurso, y `make humo` comprueba las dos mitades: el caso 10 que nadie vuelve y la llamada
se cierra, y el caso 12 que sí vuelve y la llamada sigue.

**No hay variable para apagar la confirmación.** El agente lee de vuelta lo que entendió
antes de escalar, y eso no es configurable a propósito: es una comprobación de seguridad
clínica, no una preferencia de estilo. Lo que sí está garantizado es que no retrasa la
alerta — el ticket nace en el turno de la bandera, con o sin confirmación.

---

## Qué garantiza el sistema, y con qué mecanismo

Cada garantía tiene un mecanismo y un comando que la comprueba. Si alguna deja de
sostenerse, el comando falla.

### Una bandera roja siempre produce una alerta

El ticket se crea **en el turno** en que aparece la bandera
(`EscalationService.escalar_ahora`), no al cerrar la llamada. Antes se creaba solo en
el cierre, y nada garantizaba que la llamada se cerrara: si el paciente colgaba
después de reportar secreción purulenta, no quedaba resumen ni alerta.

Tres redes cubren los tres modos en que una llamada se queda abierta:

| Cómo se pierde la llamada | Quién la cierra | `cierre_motivo` |
|---|---|---|
| El socket se cae | el handler de WebSocket | `interrumpida` |
| El cliente desaparece sin avisar | el barredor, cada 30 s | `timeout` |
| El proceso se reinicia | la recuperación al arrancar | `reinicio` |
| Se abrió y el paciente nunca habló | la recuperación, sin crear alerta | `sin_contacto` |

Cerrar con dominios sin responder **nunca** da verde: no se puede descartar lo que no
se llegó a preguntar.

`make test` · `tests/test_escalamiento_durable.py` · `make humo` casos 9 y 10.

### La alerta sale del proceso

Outbox durable en la tabla `entregas`, escrita en la misma transacción que el ticket.
El despachador (`escalation/despacho.py`) reintenta con espera creciente —30 s hasta
15 min, doce intentos— y **nunca descarta**: lo que se agota queda en estado `agotado`
con su último error, visible.

Dos canales. `archivo` va siempre y escribe la hoja de traspaso en
`data/runtime/alertas/<ticket_id>.txt`; es el que funciona recién clonado y hace de
comprobante. `webhook` se activa definiendo `CENTINELA_WEBHOOK_ALERTAS`, y firma el
cuerpo con HMAC-SHA256 en la cabecera `X-Centinela-Firma`.

### Alguien acusa recibo

`POST /api/tickets/{id}/atender` con el nombre de quien atiende. `GET /api/alertas`
lista lo que se pasó de plazo. La pestaña **Alertas** de la consola muestra las dos
cosas y permite acusar desde ahí.

### El registro dice lo que el paciente oyó

Cuando el paciente le corta la palabra al agente, el turno del agente queda truncado en el
registro con el marcador `[interrumpido]`, y la pregunta que no se llegó a oír **vuelve al
guion sin gastar un intento**.

Lo segundo es lo que importa y no se ve mirando la interfaz. La política avanza de dominio y
carga el intento cuando *construye* los fragmentos, no cuando el paciente los oye. Sin la
corrección, tres interrupciones agotarían el dominio, lo dejarían como desconocido, y un
dominio desconocido al cerrar fuerza amarillo: una alerta producida por el transporte de
audio y no por el estado del paciente.

La corrección baja a la base de datos y no se queda en memoria (`reescribir_turno`), porque
la hoja de traspaso se lee del registro durable. Se puede comparar lo uno con lo otro:
`GET /api/llamadas/{id}/traza` publica `en_memoria.turnos` y `turnos_persistidos` uno al
lado del otro, y deben decir lo mismo.

`make test` · `tests/test_deuda_interrupcion.py` · `make humo` caso 11.

### La instrucción de urgencias se oye entera, o el registro lo dice

Es la única frase del sistema cuya pérdida es un daño clínico: si el paciente no oye
*«diríjase al servicio de urgencias más cercano»*, no sabe que tiene que salir de su casa. Y
con barge-in puede cortarla.

Tres mecanismos, en orden:

1. **La llamada no cuelga antes de que suene.** El JSON del turno viaja *antes* de la voz —a
   propósito, para que el ticket no espere a que el agente hable— así que la interfaz espera a
   que su cola de audio se vacíe antes de cerrar. Hubo un fallo aquí y se veía exactamente
   como «se cayó la llamada»: colgaba al recibir el turno, parando los nodos de audio ya
   programados y cerrando el socket por el que venía el resto de la locución.
2. **Si la cortan, se repite.** El fragmento lleva `PAPEL_URGENTE`; si queda sin decir, la
   llamada no termina y `_retomar_urgencia` lo vuelve a decir. **Una vez**: insistir una
   tercera no informa a nadie y suena a máquina atascada.
3. **Y si tampoco se oye, el registro lo dice.** `urgencia_oida` queda en falso —lo afirma el
   cliente al vaciarse su cola, no el servidor al enviar— y los próximos pasos de la hoja de
   traspaso cambian: en vez de «verificar que el paciente llegó a atención», dicen que la
   indicación **no** se alcanzó a dar y hay que darla por teléfono.

`make test` · `tests/test_urgencia_oida.py` · `make humo` caso 2.

### Lo que se le leyó de vuelta, y lo que desmintió

El agente repite lo entendido antes de escalar. Los dos desenlaces quedan en el resumen
(`_centinela.confirmaciones`) y en la hoja de traspaso, y el desmentido va con mayúsculas y
antes que el resto, porque cambia cómo se lee todo lo de arriba: el hallazgo **sigue** en la
alerta —no se retira por una respuesta ambigua— pero quien llame tiene que saber que el
paciente ya lo discutió.

Lo mismo con las correcciones: `_centinela.correcciones` lleva las dos versiones con su turno,
y el nivel no baja aunque la cifra sí (`_con_piso_de_criticidad`). Dentro de una llamada la
criticidad solo sube. `make redteam`, familia `degradar_hallazgo`.

---

## Runbook

### La voz cambia de persona a mitad de llamada

Es el síntoma propio de este diseño y no es un fallo del audio: es un **fallo de caché**. La
voz del agente es una clonación pre-renderizada que se lee de disco, porque el modelo que la
genera corre a RTF 4 en CPU y no cabe en la compuerta G2. Cuando el texto que hay que decir
no está pre-renderizado, lo dice Piper — y Piper es otra persona.

```bash
curl -s localhost:8000/api/salud | python -m json.tool | grep -A 9 voz_clonada
```

| Campo | Qué leer |
|---|---|
| `disponible` | `false` significa que no hay manifiesto: el agente habla con Piper todo el tiempo |
| `cobertura_pct` | Cuántas de las frases pedidas salieron del clon |
| `fallos` | Cuántas veces cambió de voz |
| `textos_sin_clonar` | **Las frases exactas que faltaron.** Es lo que hay que renderizar |

Los tres caminos que se sabe que fallan, dichos de frente: la **respuesta del RAG** (texto
abierto del corpus), las **lecturas de vuelta de más de un dominio** («fiebre de 38.5 grados
y líquido amarillo en la herida», cuyo espacio es el producto de los dominios) y cualquier
frase del guion que se haya editado después del último render — porque la clave es el hash
del texto, y al cambiar el texto cambia la clave.

Para volver a renderizar lo que falte, con el venv aparte de la voz:

```bash
# El guion fijo
<venv-voz>/python scripts/render_clon.py --referencia <grabacion>.wav
# Las plantillas con parte variable finita: identidad por nombre y lecturas de vuelta
<venv-voz>/python scripts/render_clon.py --referencia <grabacion>.wav --derivadas
```

Y si hay que descartar la voz clonada en caliente, sin borrar nada: `CENTINELA_VOZ_CLONADA=0`.

### Una alerta no salió

```bash
curl -s localhost:8000/api/alertas | python -m json.tool | head -40
```

Mirar `entrega.pendientes` y las filas de `entregas`:

| `estado` | Qué significa | Qué hacer |
|---|---|---|
| `pendiente` | Está en la cola, con su próximo intento programado | Esperar; ver `ultimo_error` |
| `entregado` | Salió por ese canal | Nada |
| `agotado` | Doce intentos fallidos | Arreglar el destino y reencolar (abajo) |
| *(sin filas)* | El ticket es anterior al outbox | No va a salir; se atiende a mano |

Reencolar a mano lo agotado, después de arreglar el destino:

```sql
UPDATE entregas
   SET estado = 'pendiente', intentos = 0, proximo_intento_en = '2000-01-01T00:00:00+00:00'
 WHERE estado = 'agotado';
```

El despachador la recoge en el siguiente barrido, sin reiniciar nada.

### Se acumulan alertas de llamadas en las que nadie habló

Pasa si algo abre llamadas y no las usa —por ejemplo una suite apuntando al servidor
de producción—. El sistema ya no las convierte en alerta, pero si vienen de antes:

```bash
python scripts/sanear_alertas_sin_contacto.py            # enumera, no escribe
python scripts/sanear_alertas_sin_contacto.py --aplicar  # borra ticket, entrega y hoja
```

Solo toca alertas cuya llamada no tiene **ni un turno, ni transcripción, ni
`n_turnos`**. Si el paciente dijo aunque sea una palabra, la alerta se queda.

### El WebSocket se cierra con código 1006 y no se puede interrumpir al agente

Los turnos siguen funcionando —hay respaldo por HTTP— pero el barge-in desaparece, porque el
WebSocket es el único camino por el que el micrófono llega al servidor mientras el agente
habla. La consola lo dice ahora en la tira de la llamada en vez de callárselo.

La causa que nos pasó es la menos evidente posible. **Las cookies se guardan por host, no
por puerto**, así que todo lo que corra en `localhost` comparte el mismo tarro. Medido en una
máquina de desarrollo normal: 14 cookies, **9 958 bytes**, de los cuales 9 KB eran tres
tokens de Supabase de proyectos que no tienen nada que ver con esto. El navegador manda ese
`Cookie:` de 10 KB en el handshake y `websockets` corta cualquier línea de cabecera por
encima de 8 KB. En el log del servidor solo se ve `connection rejected (400 Bad Request)`; la
verdad sale con `--log-level debug`:

```
websockets.exceptions.SecurityError: line too long
```

El tope está subido a 64 KB en el arranque (`main.py::_permitir_cabeceras_largas_en_el_websocket`)
y `tests/test_cabeceras_largas.py` lo comprueba con un handshake real de 10 KB de cookies —el
efecto, no la intención: el primer parche subía una constante que en esta versión de la
librería se llama de otra forma, no dio error, y el fallo siguió igual.

Si aun así aparece:

```javascript
// en la consola del navegador, para ver cuánto pesa el tarro de cookies
document.cookie.length
```

Con más de 60 KB, abrir en una ventana de incógnito o servir en `127.0.0.1` en vez de
`localhost` —son hosts distintos y por tanto tarros distintos—. Centinela no usa ni una
cookie; borrarlas rompería los otros proyectos del usuario, así que se aceptan y no se leen.

### «El agente se corta solo» / «no se deja interrumpir»

Los dos síntomas tienen el mismo diagnóstico y está en el log. Cada resolución de una
sospecha de interrupción deja el nivel que la disparó, el umbral vigente y el eco medido:

```bash
grep -E "interrupcion_(confirmada|descartada)" registro.log | tail -20
```

| Lo que se ve | Qué significa | Qué hacer |
|---|---|---|
| Muchos `interrupcion_descartada` | El detector sospecha y el STT lo desmiente: baches de 250 ms, el agente sigue | Subir `CENTINELA_BARGEIN_MARGEN_ECO` a 2.2 |
| `interrupcion_confirmada` sin que nadie hablara | El eco se está transcribiendo como voz. Suele ser altavoz alto sin cancelación | Auriculares, o subir el margen a 3.0 |
| Ninguno de los dos, y no se puede interrumpir | `umbral` muy por encima de la voz del paciente | Comparar `umbral` con `eco_p95`; si `eco_p95` es alto, el altavoz tapa al micrófono |
| `tramas_eco: 0` | El cliente no manda tramas mientras el agente habla | Consola vieja en caché: recargar (ver la política de caché de `main.py`) |

El límite medido está publicado: `make bargein` reporta hasta qué nivel de eco funciona y
dónde deja de funcionar. Por encima de −6 dB de eco respecto a la voz, ningún ajuste de
umbral lo arregla —el altavoz está tapando al paciente— y hacen falta auriculares.

Si hay que apagarlo del todo, `CENTINELA_BARGEIN=0` deja la voz saliendo entera, que es la
conducta con la que se midió todo lo anterior.

### «Se cayó la llamada justo cuando me estaba diciendo qué hacer»

Es el fallo que dio origen a `tests/test_urgencia_oida.py`, y se ve así: el paciente reporta
fiebre alta, el agente empieza a hablar, se detiene a media locución y la interfaz cuelga. En
el registro está el texto completo; el paciente no lo oyó.

La causa está en el orden del protocolo, no en el audio. El mensaje `turno` viaja **antes** de
la voz, y viene marcado `terminada: true`; si la interfaz cuelga al recibirlo, para los nodos
de audio ya programados y cierra el socket por el que venía el resto de la locución.

Cómo diagnosticarlo si vuelve a pasar:

| Señal | Qué significa |
|---|---|
| El registro tiene el texto completo y el paciente oyó solo el principio | La interfaz colgó antes de tiempo: el turno terminado no debe cerrar, lo cierra `fin_voz` con la cola vacía |
| Llega `fin_llamada` pero la consola sigue en «El agente habla» | Un trozo de voz no se decodificó; el perro guardián cierra tras 8 s **sin que la voz avance** y lo dice en el registro. Mide silencio, no duración total: con un plazo fijo cortaba el cierre rojo, que son 29,5 s de audio, en el segundo 20 — justo antes de «diríjase al servicio de urgencias» |
| `urgencia_oida: false` en el resumen | El paciente cortó la instrucción. Es dato, no fallo: la hoja de traspaso ya avisa de que hay que darla por teléfono |

### El arranque tarda más de lo normal

La recuperación de llamadas colgadas está acotada a 50 por arranque
(`MAX_RECUPERAR_AL_ARRANCAR`). El resto lo recoge el barredor con el servidor ya
sirviendo. Si el arranque se alarga, mirar cuántas quedan:

```sql
SELECT COUNT(*) FROM llamadas WHERE terminada_en IS NULL;
```

### Dejar la consola limpia para grabar o demostrar

Correr las suites deja rastro legítimo: `make humo` abre llamadas, `make redteam` abre
cuarenta y dos, y cada una que escala produce su alerta. Tras un día de desarrollo la
bandeja tiene cientos de alertas sin acuse —todas correctas, todas de pruebas— y una
consola con 300 alertas rojas fuera de plazo no se lee como un sistema que funciona: se
lee como un sistema en crisis.

```bash
make demo                                  # enumera, no escribe
python scripts/preparar_demo.py --aplicar  # respalda y vacía
```

Respalda con `.backup` antes de borrar, vacía las tablas de operación, y borra las hojas
entregadas y el registro de métricas por turno. **No toca** el índice del corpus, los
documentos subidos, el caché de audio ni los modelos.

**El orden importa.** Vaciar el estado invalida la muestra que sostiene las métricas de
la rúbrica, así que se limpia *antes* de medir y no después:

```bash
python scripts/preparar_demo.py --aplicar
make up && make humo && make runtime && make metricas && make cifras
```

La última comprobación es la que cierra el círculo: `make cifras` falla si algún número
del README o del informe dejó de cuadrar con la nueva medición.

### Respaldo

La base de datos de llamadas es el registro clínico. `.backup` de SQLite es seguro con
el servidor corriendo —a diferencia de copiar el archivo—:

```bash
sqlite3 data/runtime/llamadas.db ".backup 'respaldo-$(date +%F).db'"
```

Qué hay dentro: `llamadas` (con el resumen con forma FHIR y la transcripción),
`tickets`, `entregas`, `turnos`, `mediciones`, `incidentes`. El índice del corpus vive
aparte, en `data/index/`, y se reconstruye desde los PDF con `make index`.

### Retención

No hay política automática, a propósito: cuánto se guarda un registro clínico lo
decide la institución, no el software. Los `turnos` son los que más crecen y son los
únicos prescindibles una vez cerrada la llamada, porque su información ya está en el
resumen y en la transcripción:

```sql
DELETE FROM turnos
 WHERE llamada_id IN (SELECT llamada_id FROM llamadas
                       WHERE terminada_en < date('now', '-90 days'));
```

### Rotar el token

```bash
CENTINELA_TOKEN=nuevo-secreto make up
```

La consola lo pide en el primer 401 y lo guarda en `sessionStorage`, así que se vuelve
a pedir al cerrar la pestaña. En un puesto compartido eso es lo que se quiere.

**Tiene que ser una sola palabra**, sin espacios ni comas. El canal de voz lo lleva en
el subprotocolo del WebSocket (`Sec-WebSocket-Protocol`) y no en la URL, porque un token
en una URL queda escrito en el log de acceso de cualquier proxy que haya en medio. Un
subprotocolo es un *token* de HTTP y no admite separadores. Si el configurado no cumple,
el arranque avisa con `token_no_transportable` en vez de dejar un canal de voz que no
conecta sin decir por qué.

### Qué protege exactamente

| Con `CENTINELA_TOKEN` puesto | |
|---|---|
| Pide token | Subir y borrar documentos · atender una alerta · correr las suites · **la llamada entera**: `POST /api/llamadas`, `/turno`, `/audio`, `/cerrar` y `ws/llamada/{id}` |
| Queda abierto | Todo lo que solo lee: `/api/salud`, `/api/reglas`, `/api/metricas`, `/api/documentos`, `/api/llamadas`, `/api/tickets`, las trazas |

Que la llamada esté protegida **entera** y no solo al abrirla es la corrección de un
hueco real. Antes, `POST /api/llamadas` pedía credencial y `/turno`, `/audio`, `/cerrar`
y el WebSocket no, así que el `llamada_id` hacía de credencial de facto — y no lo era,
porque `GET /api/llamadas` es de lectura y lo entrega. Medido contra el servidor en
marcha, sin presentar nada: se leyó el id de una llamada **en curso** del listado, se la
condujo a ROJO y se creó su ticket. Quien conduce una llamada escribe en el registro
clínico de un paciente; eso es modificar. El listado sigue abierto y el id sigue ahí, y
está bien: la diferencia es que ya no es una llave. Lo fija `tests/test_acceso.py`.

---

## Qué mirar en los logs

Una línea JSON por evento, en stderr, con `llamada_id` como campo de primera clase
(`obs/log.py`). Los eventos que importan en operación:

| Evento | Cuándo | Nivel |
|---|---|---|
| `alerta_creada` | Bandera roja detectada en un turno | info |
| `alerta_entregada` | Salió por un canal | info |
| `alerta_no_entregada` | Un canal falló; se reintentará | error |
| `alerta_atendida` | Alguien acusó recibo | info |
| `interrupcion_confirmada` | El paciente cortó al agente; con rms, umbral y eco | info |
| `interrupcion_descartada` | Era una tos: el agente recupera la voz y sigue | info |
| `interrupcion_sin_resolver` | La ventana era corta para decidir; se vuelve a mirar con más audio | info |
| `turno_cerrado_por_completitud` | El turno cerró antes del techo, con el motivo | info |
| `silencio_del_paciente` | Un peldaño de la escalera de silencio, con cuál y a los cuántos segundos | info |
| `cierre_por_silencio` | El paciente dejó de contestar y la llamada se cerró, con el motivo que va a la hoja | info |
| `suelo_liberado_sin_confirmacion` | El cliente no confirmó que la voz sonó y el servidor soltó el suelo por su cuenta | aviso |
| `calibracion_recibida` | El cliente midió su sala (piso y umbral de voz) | info |
| `llamada_cerrada_por_el_sistema` | Cierre forzado, con su motivo | info |
| `llamada_cerrada_por_inactividad` | Expiró el plazo sin turnos | info |
| `llamadas_recuperadas_al_arrancar` | Cuántas quedaron de un proceso anterior | info |
| `acceso_rechazado` | 401 en un endpoint protegido, o `canal=ws` si fue el canal de voz | aviso |
| `token_no_transportable` | `CENTINELA_TOKEN` tiene espacios: el canal de voz no lo puede llevar | aviso |
| `subida_rechazada_por_tamano` | PDF por encima del tope | aviso |

No se registra lo que dijo el paciente. La transcripción vive en la base de datos de
llamadas, que es el registro clínico; duplicarla en un log de operación la esparce por
sitios con retenciones distintas.

---

## Fronteras de alcance, dichas de frente

Tres cosas que un despliegue clínico real necesita y que este sistema no tiene. Están
acá y no escondidas porque la diferencia entre un límite declarado y un límite
descubierto en producción es toda la diferencia.

**Identidad por persona.** `CENTINELA_TOKEN` es un secreto compartido. Sirve para que
la API no quede abierta, y no sirve para lo que un sistema clínico necesita de verdad:
saber **qué persona** atendió cada alerta con una identidad que no se pueda prestar.
Eso es SSO y una tabla de usuarios con roles, y no está.

**Telefonía.** El canal es el navegador: micrófono y WebSocket. No marca a un teléfono.
El salto es un adaptador de transporte —los *media streams* de un proveedor hablan el
mismo PCM16 a 16 kHz que ya consume `ws_llamada`— pero no está construido ni probado.

**Cifrado en reposo.** SQLite sin cifrar en el disco del servidor. Para datos clínicos
reales hace falta cifrado de volumen o de base, y una decisión sobre dónde vive la
clave.

**Cancelación de eco propia.** La interrupción se apoya en el `echoCancellation` del
navegador más un umbral que se calcula sobre el eco medido. Funciona hasta que el eco queda
6 dB por debajo de la voz del paciente; por encima de eso el altavoz tapa al micrófono y el
límite está publicado en `make bargein`, no escondido. Un cancelador propio —filtro
adaptativo contra la señal de referencia, que el servidor sí conoce porque él la envió—
cubriría el caso del altavoz abierto a todo volumen, y no está.

**Solapamiento real.** La interrupción es excluyente: uno de los dos calla. En una
conversación humana hay medio segundo en que los dos hablan y ninguno se detiene.

### La latencia publicada no cuadra con la que se siente

Tres cosas distintas se llaman "latencia" aquí, y confundirlas explica casi cualquier
discrepancia:

| Cifra | Qué mide | Dónde sale |
|---|---|---|
| `ms_hasta_primer_audio` | desde que el VAD cierra hasta el primer byte de audio | `/api/metricas`, es la que nombra la rúbrica |
| extremo a extremo | lo anterior **más el endpointing** (450–900 ms) | `runtime.json → extremo_a_extremo` |
| por camino | lo mismo, separado por cache / modelo / corpus / voz | `runtime.json → por_camino` |

Dos reglas para no publicar una cifra que no describe nada:

**Nunca una sola cifra sobre la mezcla.** Un turno del guion sale del caché en 0.6 ms y uno
que consulta el corpus tarda segundos: cuatro órdenes de magnitud. El agregado se mueve con
la proporción de preguntas del guion que tenga la muestra, no con el sistema.

**Nunca cruzando configuraciones.** Cada medición anota su `stt` (por ejemplo
`medium/cuda`). Mezclar el histórico de `small/cpu` con el de `medium/cuda` daba un P95 de
voz de 13 732 ms cuando la configuración vigente está en 2 198. Los turnos anteriores al
campo salen como `sin registrar` y se publican aparte, porque no se sabe con qué se
midieron.

`make runtime` avisa si el camino de voz tiene menos de 20 turnos. Para llenarlo:

```bash
python -m eval.conversacion_voz --url ws://127.0.0.1:8000   # 5 turnos de voz por corrida
```

Ese arnés **tiene que mandar `fin_reproduccion`** como hace el navegador. Es lo único que
libera el suelo en el servidor: mientras el agente tiene la palabra las tramas del
micrófono son eco por definición y no entran en la sesión. Un cliente que no lo manda
pierde el turno siguiente entero, con `audio de 0.00s`, y el siguiente funciona —porque el
camino `sin_habla` también libera el suelo—. El síntoma alterna, y esa alternancia es la
firma.

### El STT transcribe en CPU teniendo GPU (y no lo dice)

Es el fallo más caro de detectar que ha tenido este sistema, porque **no produce ningún
error**. La escalera de configuraciones de `stt/whisper.py` prueba `medium/cuda`,
`small/cuda` y `small/cpu` en ese orden, con una inferencia real, y baja un peldaño cuando
uno falla. Si CUDA falla, acaba en CPU y sigue atendiendo llamadas: más lento y con más
error, sin una línea de log que lo llame problema.

Cómo se comprueba, en un solo sitio:

```bash
curl -s localhost:8000/api/salud | python -c "import json,sys; s=json.load(sys.stdin)['stt']; print(s['modelo'], s['dispositivo'], 'degradado:', s['degradado_a_cpu'])"
```

`medium cuda/int8_float16 degradado: False` es lo esperado en una máquina con GPU. Si sale
`small cpu/int8 degradado: True`, mira `intentos` en el mismo bloque: dice qué peldaño
falló y con qué error.

**La causa que ya ocurrió** fue `Library cublas64_12.dll is not found or cannot be loaded`
en la primera inferencia, con la DLL presente en el disco. Viene en los wheels
`nvidia-cublas-cu12` y `nvidia-cudnn-cu12`, y en Windows Python no la busca ahí:
`habilitar_dlls_cuda()` pone esos directorios en el `PATH` del proceso antes de cargar el
modelo. Si el diagnóstico vuelve, comprueba que los wheels estén instalados:

```bash
python -c "import sysconfig,pathlib; p=pathlib.Path(sysconfig.get_paths()['purelib'])/'nvidia'; print([d.name for d in p.iterdir()] if p.is_dir() else 'sin wheels de NVIDIA')"
```

**Sin GPU el sistema funciona, y no es equivalente.** Medido sobre las 18 grabaciones de
`eval/audios`:

| | latencia | WER medio | dato clínico mal | repreguntas |
|---|---:|---:|---:|---:|
| `medium` en GPU | 231 ms | 0.053 | 0 | 0 de 18 |
| `small` en CPU | 1 054 ms | 0.126 | 1 | 1 de 18 |

Se atiende igual: un peldaño más abajo es mejor que no atender. Pero el informe no debe
publicar las cifras de GPU si la máquina está en CPU, y por eso `scripts/bench_voz.py`
declara el dispositivo que midió.

### Un turno tarda segundos y el desglose por etapa no explica por qué

Mira los contadores de invocación del STT en `GET /api/salud`:

```
"stt": { "invocaciones": 3, "segundos_transcritos": 9.4 }
```

Compáralos con el audio que de verdad hubo en la llamada. Si los segundos transcritos son
mucho más que el audio real, el turno no es lento: **compite por la GPU con trabajo que no
hacía falta**. El desglose por etapa no lo distingue, porque a la etapa `stt` de un turno le
aparece el tiempo que otro dejó encolado.

Es exactamente el defecto que estos contadores destaparon. Una llamada con **7.7 s de audio
real transcribía 172.6 s en 55 invocaciones** —22 veces— porque cada veredicto del detector
de barge-in lanzaba su propia comprobación sin mirar si ya había otra en vuelo, y cada una
transcribía el candidato completo, que crece mientras el paciente habla. El turno posterior a
la interrupción tardaba 7 852 ms cuando ese mismo audio, transcrito aislado, cuesta 480 ms.
Con la guarda de una comprobación a la vez: 3 invocaciones, 9.4 s, y el turno en 531 ms.

Los contadores existen por esto. Sin ellos el síntoma apunta al STT, que era inocente.
