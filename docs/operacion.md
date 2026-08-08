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
| `CENTINELA_TOKEN` | *(vacío)* | Si se define, protege los endpoints que modifican algo |
| `CENTINELA_MAX_MB_DOC` | `64` | Tope de tamaño de un PDF subido |
| `CENTINELA_BARGEIN` | `1` | El paciente puede cortarle la palabra al agente. A `0`, la voz sale entera |
| `CENTINELA_BARGEIN_MARGEN_ECO` | `1.8` | Cuánto por encima del eco medido hay que hablar para cortar |
| `CENTINELA_BARGEIN_MS_CONF` | `250` | Cuánto audio se acumula antes de preguntarle al STT si eso era voz |
| `CENTINELA_CIERRE_ADAPTATIVO` | `1` | El turno cierra en cuanto la respuesta se sostiene sola. A `0`, siempre el techo |
| `CENTINELA_CIERRE_MIN_MS` | `450` | Plazo mínimo: por debajo no se cierra ni con la respuesta más clara |
| `CENTINELA_LLM_MODEL` | `phi3.5:3.8b-mini-instruct-q4_K_M` | Modelo declarado (compuerta G3) |
| `CENTINELA_DIR_RUNTIME` | `data/runtime` | Dónde vive la base de datos de llamadas |

El `1.8` del margen de eco lo eligió el barrido de `make bargein-barrido`, y
`tests/test_bargein.py` comprueba que la configuración no se separe del módulo: tener el
número medido en un sitio y el que corre en otro es peor que no medirlo —pasó, y el test
existe por eso. Subirlo hace más difícil interrumpir y más raro el bache falso; bajarlo, lo
contrario.

**No hay variable de techo del turno.** El techo de 900 ms lo pone el VAD del navegador,
que es quien mide el silencio en el micrófono (`web/app.js`, `VAD.msSilencioParaCerrar`).
Publicar aquí un `CENTINELA_CIERRE_MAX_MS` sería ofrecer un mando desconectado.

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

---

## Runbook

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
| `turno_cerrado_por_completitud` | El turno cerró antes del techo, con el motivo | info |
| `calibracion_recibida` | El cliente midió su sala (piso y umbral de voz) | info |
| `llamada_cerrada_por_el_sistema` | Cierre forzado, con su motivo | info |
| `llamada_cerrada_por_inactividad` | Expiró el plazo sin turnos | info |
| `llamadas_recuperadas_al_arrancar` | Cuántas quedaron de un proceso anterior | info |
| `acceso_rechazado` | 401 en un endpoint protegido | aviso |
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
