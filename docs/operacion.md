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
| `CENTINELA_LLM_MODEL` | `phi3.5:3.8b-mini-instruct-q4_K_M` | Modelo declarado (compuerta G3) |
| `CENTINELA_DIR_RUNTIME` | `data/runtime` | Dónde vive la base de datos de llamadas |

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

### El arranque tarda más de lo normal

La recuperación de llamadas colgadas está acotada a 50 por arranque
(`MAX_RECUPERAR_AL_ARRANCAR`). El resto lo recoge el barredor con el servidor ya
sirviendo. Si el arranque se alarga, mirar cuántas quedan:

```sql
SELECT COUNT(*) FROM llamadas WHERE terminada_en IS NULL;
```

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
