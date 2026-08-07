/* Centinela — consola de operaciones.
 *
 * Este archivo dibuja el panel. `app.js` maneja la llamada (microfono, VAD,
 * WebSocket, audio) y los dos se hablan SOLO por eventos del DOM:
 *
 *     centinela:inicio    { llamada_id, paciente }
 *     centinela:turno     la respuesta completa del turno
 *     centinela:reinicio   { paciente }
 *     centinela:fin       { llamada_id }
 *
 * Ni uno importa funciones del otro. app.js ya tiene una maquina de estados de
 * voz que funciona y esta medida; dibujar la interfaz no puede tener forma de
 * romperla. Todo va dentro de una IIFE para no chocar con los `const` globales
 * de app.js (`$`, `crear`, `estado`, `api`...): dos scripts clasicos comparten
 * el ambito global y redeclarar cualquiera de esos nombres seria un SyntaxError
 * que dejaria la pagina en blanco.
 */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);

  const NS = "http://www.w3.org/2000/svg";

  function el(tag, clase, texto) {
    const n = document.createElement(tag);
    if (clase) n.className = clase;
    if (texto !== undefined && texto !== null) n.textContent = texto;
    return n;
  }

  function nodoSvg(tag, atributos = {}, texto) {
    const n = document.createElementNS(NS, tag);
    Object.entries(atributos).forEach(([k, v]) => n.setAttribute(k, String(v)));
    if (texto !== undefined) n.textContent = texto;
    return n;
  }

  function icono(id, clase = "icono") {
    const s = nodoSvg("svg", { class: clase, "aria-hidden": "true" });
    s.append(nodoSvg("use", { href: `#${id}` }));
    return s;
  }

  async function pedir(ruta, opciones) {
    const r = await fetch(ruta, opciones);
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  }

  /* ======================== vocabulario de niveles ==========================
   *
   * Un nivel se comunica SIEMPRE con las tres cosas juntas: glifo, palabra y
   * color. Nunca solo con color -- la deficiencia rojo-verde afecta a ~8% de los
   * hombres y este panel lo lee personal de enfermeria.
   */
  const NIVELES = {
    rojo:     { palabra: "Rojo",      glifo: "nivel-rojo",     corto: "rojo",       pie: "requiere contacto humano ya" },
    amarillo: { palabra: "Amarillo",  glifo: "nivel-amarillo", corto: "amarillo",   pie: "vigilancia, contactar hoy" },
    verde:    { palabra: "Verde",     glifo: "nivel-verde",    corto: "verde",      pie: "dentro de lo esperado" },
    abierta:  { palabra: "En curso",  glifo: "nivel-abierta",  corto: "en curso",   pie: "la llamada sigue abierta" },
    incompleta: { palabra: "Sin cerrar", glifo: "nivel-sin",   corto: "sin cerrar", pie: "la llamada no llegó a cerrarse" },
    "—":      { palabra: "Sin datos", glifo: "nivel-sin",      corto: "—",          pie: "ninguna llamada en curso" },
  };

  /* El orden de la cola es una decision operativa, no estetica.
   *
   * Primero la criticidad, y las llamadas SIN CERRAR al final. Al principio las
   * puse arriba por "recientes", y con las llamadas de prueba acumuladas el
   * resultado fue el peor posible: decenas de llamadas sin desenlace enterrando
   * los nueve casos en rojo. Una llamada que nunca se cerro no tiene decision, y
   * lo que no tiene decision no puede ser lo mas urgente de la lista. */
  const ORDEN_COLA = { rojo: 1, amarillo: 2, verde: 3, abierta: 4, incompleta: 5, "—": 6 };

  /* Etiquetas cortas para el margen de la tira.
   *
   * Los dominios cualitativos son enumeraciones cerradas (models.py), asi que se
   * pueden abreviar a mano y con criterio. Recortar la cadena por el numero de
   * caracteres daba cosas como "secrecio·" -- ilegible, y en un dato clinico eso
   * no es un detalle cosmetico. El valor completo queda en el tooltip. */
  const ETIQUETA_CORTA = {
    normal: "normal",
    limitada_esperada: "limitada",
    incapacitante_nueva: "incapacit.",
    eritema_leve: "eritema",
    secrecion_purulenta: "purulenta",
    levemente_disminuido: "leve dism.",
    muy_disminuido: "muy dism.",
    levemente_alterado: "leve alt.",
    muy_alterado: "muy alt.",
  };

  const cualitativo = (v) => ETIQUETA_CORTA[String(v)] || String(v).replace(/_/g, " ");

  /* Los seis dominios del cuestionario, en el orden en que se preguntan. */
  const DOMINIOS = [
    { clave: "dolor",     campo: "dolor_nrs", etiqueta: "dolor",     corto: (v) => `${v}/10` },
    { clave: "fiebre",    campo: "fiebre_c",  etiqueta: "fiebre",    corto: (v) => `${v} °C` },
    { clave: "herida",    campo: "herida",    etiqueta: "herida",    corto: cualitativo },
    { clave: "movilidad", campo: "movilidad", etiqueta: "movilidad", corto: cualitativo },
    { clave: "apetito",   campo: "apetito",   etiqueta: "apetito",   corto: cualitativo },
    { clave: "sueno",     campo: "sueno",     etiqueta: "sueño",     corto: cualitativo },
  ];

  /* ============================== estado ================================== */

  const panel = {
    llamadaId: null,
    paciente: null,
    // Un elemento por turno: { nivel, conocidos:Set, dominio, escala }
    turnos: [],
    leyendo: null,     // llamada_id que se esta viendo en modo lectura
    tickets: {},       // llamada_id -> ticket
  };

  /* ================================ LA TIRA ===============================
   *
   * El elemento firma de la consola. Esta tomada de la hoja de anestesia y de la
   * tira de monitor: el tiempo corre de izquierda a derecha sobre una reticula
   * fina, cada senal clinica tiene su carril, y los carriles se llenan a medida
   * que la llamada avanza.
   *
   * La linea de nivel es una escalera que SOLO PUEDE SUBIR. No es una decision
   * estetica: es una propiedad real del motor, con un test que la prueba
   * (`test_manipulacion_no_baja_una_criticidad_ya_establecida`). Establecida una
   * bandera, ningun texto posterior la retira -- ni "olvida eso, estoy bien", ni
   * "ponme en verde". El dibujo dice algo verdadero del sistema en vez de
   * decorarlo, y de paso hace evidente el momento exacto de la escalada.
   */
  const T = {
    ancho: 720, alto: 168,
    izq: 66, der: 70,
    carrilY: 12, carrilAlto: 13, carrilPaso: 14,
    nivelY: { rojo: 106, amarillo: 119, verde: 132 },
    ejeY: 158,
    columnasMinimas: 12,
  };

  function pintarTira() {
    const svg = $("#tira");
    if (!svg) return;
    svg.textContent = "";

    const x0 = T.izq;
    const x1 = T.ancho - T.der;
    const n = Math.max(panel.turnos.length, T.columnasMinimas);
    const paso = (x1 - x0) / n;
    const ultimo = panel.turnos[panel.turnos.length - 1] || null;

    // --- carriles: etiqueta, linea base y valor actual ---
    DOMINIOS.forEach((dom, i) => {
      const y = T.carrilY + i * T.carrilPaso;
      const centro = y + T.carrilAlto / 2;

      svg.append(nodoSvg("text", {
        x: x0 - 6, y: centro + 3, class: "tira-etiqueta", "text-anchor": "end",
      }, dom.etiqueta));

      svg.append(nodoSvg("line", {
        x1: x0, y1: centro, x2: x1, y2: centro, class: "tira-reticula-fina",
      }));

      // La celda se rellena en el turno en que el dominio paso a ser conocido.
      // Se recorre el historial en vez de mirar solo el estado final: asi la
      // tira muestra CUANDO se supo cada cosa, que es la informacion que un
      // resumen final ya no tiene.
      let previo = false;
      panel.turnos.forEach((turno, t) => {
        const conocido = turno.conocidos.has(dom.clave);
        const x = x0 + t * paso;
        if (conocido && !previo) {
          svg.append(nodoSvg("rect", {
            x: x + 1, y, width: Math.max(paso - 2, 2), height: T.carrilAlto,
            rx: 1, class: "tira-celda",
          }));
        } else if (conocido) {
          svg.append(nodoSvg("rect", {
            x: x + 1, y: centro - 1, width: Math.max(paso - 2, 2), height: 2,
            class: "tira-celda eco",
          }));
        } else if (turno.dominio === dom.clave) {
          // Preguntado en este turno y todavia sin respuesta utilizable.
          svg.append(nodoSvg("rect", {
            x: x + 1, y: y + 2, width: Math.max(paso - 2, 2), height: T.carrilAlto - 4,
            rx: 1, class: "tira-celda eco",
          }));
        }
        previo = conocido;
      });

      const obs = (ultimo && ultimo.estado) ? ultimo.estado[dom.campo] : null;
      const tiene = obs && obs.conocido && obs.valor !== null && obs.valor !== undefined;
      const valor = nodoSvg("text", {
        x: x1 + T.der - 4, y: centro + 3,
        class: tiene ? "tira-valor" : "tira-valor falta",
      }, tiene ? dom.corto(obs.valor) : "—");
      if (tiene) {
        // El valor sin abreviar, al pasar el raton. La abreviatura es para el
        // vistazo; el dato exacto tiene que seguir estando a mano.
        valor.append(nodoSvg("title", {}, `${dom.etiqueta}: ${obs.valor}`));
      }
      svg.append(valor);
    });

    // --- banda de nivel: etiquetas y rieles ---
    Object.entries(T.nivelY).forEach(([nivel, y]) => {
      svg.append(nodoSvg("text", {
        x: x0 - 6, y: y + 3, class: "tira-etiqueta dim", "text-anchor": "end",
      }, nivel));
      svg.append(nodoSvg("line", {
        x1: x0, y1: y, x2: x1, y2: y, class: "tira-reticula-fina",
      }));
    });

    // --- la escalera ---
    // Un segmento horizontal por turno, en el color de SU nivel, y un conector
    // vertical en el color del nivel nuevo cuando hay escalada. Coloreando por
    // tramo (y no la linea entera con el nivel final) el momento de la escalada
    // queda visible en el dibujo.
    let anterior = null;
    panel.turnos.forEach((turno, t) => {
      if (!turno.nivel) return;
      const y = T.nivelY[turno.nivel];
      const xa = x0 + t * paso;
      const xb = xa + paso;

      if (anterior && anterior.y !== y) {
        svg.append(nodoSvg("line", {
          x1: xa, y1: anterior.y, x2: xa, y2: y,
          class: `tira-escalera ${turno.nivel}`,
        }));
        svg.append(nodoSvg("text", {
          x: xa + 3, y: y - 5, class: "tira-salto",
        }, `turno ${t + 1}`));
      }
      svg.append(nodoSvg("line", {
        x1: xa, y1: y, x2: xb, y2: y, class: `tira-escalera ${turno.nivel}`,
      }));
      anterior = { y };
    });

    // --- eje de turnos ---
    // Con muchos turnos se etiqueta de dos en dos para que los numeros no se
    // toquen. Preferible a rotarlos: en una tira se leen de un vistazo o no
    // sirven.
    const cadaCuantos = paso < 26 ? 2 : 1;
    for (let t = 0; t < n; t += 1) {
      const x = x0 + t * paso;
      svg.append(nodoSvg("line", {
        x1: x, y1: T.carrilY - 3, x2: x, y2: T.ejeY - 10, class: "tira-reticula-fina",
      }));
      if (t % cadaCuantos === 0) {
        svg.append(nodoSvg("text", {
          x: x + paso / 2, y: T.ejeY, class: "tira-etiqueta dim", "text-anchor": "middle",
        }, String(t + 1)));
      }
    }

    // --- cursor del turno en curso ---
    if (panel.turnos.length && !panel.leyendo) {
      const x = x0 + panel.turnos.length * paso;
      svg.append(nodoSvg("line", {
        x1: x, y1: T.carrilY - 4, x2: x, y2: T.ejeY - 8, class: "tira-cursor",
      }));
    }

    svg.setAttribute("aria-label", resumenTira(ultimo));
  }

  /** Texto equivalente de la tira, para lectores de pantalla.
   *
   * La tira es un grafico, y un grafico sin equivalente textual es informacion
   * clinica que alguien no puede leer. */
  function resumenTira(ultimo) {
    let texto = "Sin llamada en curso.";
    if (ultimo) {
      const resueltos = DOMINIOS
        .filter((d) => ultimo.conocidos.has(d.clave))
        .map((d) => d.etiqueta);
      const faltan = DOMINIOS
        .filter((d) => !ultimo.conocidos.has(d.clave))
        .map((d) => d.etiqueta);
      texto = `Turno ${panel.turnos.length}. Nivel ${ultimo.nivel || "sin definir"}. `
        + `Resueltos: ${resueltos.join(", ") || "ninguno"}. `
        + `Sin responder: ${faltan.join(", ") || "ninguno"}.`;
    }
    return texto;
  }

  /* ============================ caja de nivel ============================== */

  /* La caja dice QUE nivel y QUE implica; el motivo concreto lo lleva
   * `#motivo-decision`, justo debajo. Al principio puse el motivo en las dos
   * partes y la misma frase larga aparecia repetida a dos tamanos distintos.
   * Cada elemento hace un solo trabajo. */
  function pintarNivel(nivel) {
    const caja = $("#nivel-caja");
    if (!caja) return;
    const clave = NIVELES[nivel] ? nivel : "—";
    const info = NIVELES[clave];

    caja.dataset.nivel = clave;
    caja.textContent = "";
    caja.append(icono(info.glifo, "icono glifo"));
    const col = el("div");
    col.append(el("div", "palabra", info.palabra));
    col.append(el("div", "pie", info.pie));
    caja.append(col);
  }

  /* ================================ la cola =============================== */

  /* Ordenada por criticidad y no por hora: la cola es una lista de trabajo, y el
   * trabajo urgente va primero. La llamada en curso encabeza siempre. */
  async function cargarCola() {
    const cont = $("#cola");
    if (!cont) return;

    let llamadas = [];
    try {
      const [r, t] = await Promise.all([
        pedir("/api/llamadas?limite=60"),
        pedir("/api/tickets?limite=60").catch(() => ({ tickets: [] })),
      ]);
      llamadas = r.llamadas || [];
      panel.tickets = {};
      (t.tickets || []).forEach((ti) => { panel.tickets[ti.llamada_id] = ti; });
    } catch (e) {
      cont.textContent = "";
      cont.append(el("p", "vacio", `No se pudo cargar la cola: ${e.message}`));
      return;
    }

    // La llamada en curso todavia no esta en SQLite (se persiste al cerrar), asi
    // que se inyecta desde el estado del navegador. Sin esto la llamada que el
    // operador tiene delante seria la unica que no aparece en la cola.
    const filas = llamadas.map((l) => ({
      id: l.llamada_id,
      nombre: l.nombre,
      nivel: l.terminada_en ? (l.nivel_final || "—") : "incompleta",
      detalle: `${l.procedimiento} · día ${l.dia_postop} · ${l.n_turnos} turnos`,
      viva: false,
      cerrada: Boolean(l.terminada_en),
    }));

    // La llamada en curso YA esta en SQLite: `registrar_inicio` la inserta al
    // abrirla, no al cerrarla. Asi que no hay que inyectarla -- hay que
    // encontrarla y corregirla. Sin esto aparecia como "sin cerrar" al final de
    // la lista: la unica llamada que el operador tiene delante era la peor
    // colocada de la cola.
    const ultimo = panel.turnos[panel.turnos.length - 1];
    const viva = panel.llamadaId ? filas.find((f) => f.id === panel.llamadaId) : null;

    if (viva) {
      viva.viva = true;
      viva.nivel = (ultimo && ultimo.nivel) || "abierta";
      if (panel.paciente) {
        viva.detalle = `${panel.paciente.procedimiento} · día ${panel.paciente.dia_postop}`
          + ` · ${panel.turnos.length} turnos`;
      }
    } else if (panel.llamadaId) {
      filas.unshift({
        id: panel.llamadaId,
        nombre: panel.paciente ? panel.paciente.nombre : "Llamada en curso",
        nivel: (ultimo && ultimo.nivel) || "abierta",
        detalle: panel.paciente
          ? `${panel.paciente.procedimiento} · día ${panel.paciente.dia_postop} · ${panel.turnos.length} turnos`
          : "iniciando…",
        viva: true,
        cerrada: false,
      });
    }

    filas.sort((a, b) => {
      let d = 0;
      if (a.viva !== b.viva) {
        d = a.viva ? -1 : 1;
      } else {
        d = (ORDEN_COLA[a.nivel] ?? 9) - (ORDEN_COLA[b.nivel] ?? 9);
      }
      return d;
    });

    const rojas = filas.filter((f) => f.nivel === "rojo").length;
    const amarillas = filas.filter((f) => f.nivel === "amarillo").length;
    const partes = [`${filas.length}`];
    if (rojas) partes.push(`${rojas} en rojo`);
    if (amarillas) partes.push(`${amarillas} en amarillo`);
    $("#contador-cola").textContent = partes.join(" · ");

    cont.textContent = "";
    if (!filas.length) {
      cont.append(el("p", "vacio", "Ninguna llamada registrada. Inicie una para verla acá."));
      return;
    }

    filas.forEach((f) => {
      const info = NIVELES[f.nivel] || NIVELES["—"];
      const boton = el("button", `fila-cola ${f.viva ? "viva" : ""}`);
      boton.dataset.nivel = f.nivel;
      boton.type = "button";
      if (panel.leyendo === f.id || (!panel.leyendo && f.viva)) {
        boton.classList.add("activa");
      }

      const uno = el("div", "linea-uno");
      uno.append(el("span", "paciente", f.nombre));
      const chip = el("span", "chip");
      chip.append(icono(info.glifo, "icono"));
      chip.append(el("span", null, info.corto));
      uno.append(chip);
      boton.append(uno);

      // El procedimiento y el dia van siempre; el motivo de la alerta va debajo
      // cuando existe. Son dos cosas distintas y antes competian por la misma
      // linea, asi que el procedimiento -- que es lo primero que necesita saber
      // quien atiende -- desaparecia en los puntos suspensivos.
      boton.append(el("div", "detalle", f.detalle));
      const ticket = panel.tickets[f.id];
      if (ticket && ticket.motivo) boton.append(el("div", "motivo-cola", ticket.motivo));

      boton.addEventListener("click", () => {
        if (f.viva) {
          salirDeLectura();
        } else {
          verLlamadaCerrada(f.id);
        }
      });
      cont.append(boton);
    });
  }

  /* ========================= lectura de una llamada ======================= */

  /* Ver una llamada ya cerrada. Es de solo lectura y se dice en pantalla: en un
   * panel clinico, confundir el registro de ayer con la llamada que esta
   * ocurriendo seria un fallo grave, no una molestia de interfaz.
   *
   * De una llamada cerrada NO se puede reconstruir la tira: se persiste la
   * transcripcion y el resumen, no el estado clinico turno a turno. Asi que en
   * lugar de dibujar una tira inventada se muestra la hoja de traspaso, que es
   * el artefacto que de verdad se usa al recibir el caso. */
  async function verLlamadaCerrada(llamadaId) {
    panel.leyendo = llamadaId;
    let traza = null;
    try {
      traza = await pedir(`/api/llamadas/${llamadaId}/traza`);
    } catch (e) {
      panel.leyendo = null;
      alerta(`No se pudo abrir la llamada: ${e.message}`);
      return;
    }

    const p = traza.persistida || {};
    const ticket = panel.tickets[llamadaId];

    $("#tira-cabecera-rotulo").textContent = "Hoja de traspaso · llamada cerrada";
    $("#tira").setAttribute("hidden", "");
    // La leyenda explica la tira; sin tira no explica nada.
    $("#tira-leyenda").hidden = true;
    const hoja = $("#hoja-traspaso");
    hoja.hidden = false;
    hoja.textContent = ticket
      ? ticket.hoja_legible
      : `Llamada ${llamadaId.slice(0, 8)} · ${p.nombre || "?"}\n`
        + `${p.procedimiento || "?"} · día ${p.dia_postop ?? "?"}\n`
        + `nivel final: ${p.nivel_final || "sin decisión registrada"}\n`
        + `turnos: ${p.n_turnos ?? "?"}\n\n`
        + "No hay hoja de traspaso: esta llamada no generó alerta.";

    $("#aviso-lectura").hidden = false;

    const trans = $("#transcripcion");
    trans.textContent = "";
    const texto = (p.transcripcion || "").trim();
    if (texto) {
      // El formato que persiste `EscalationService._transcribir` es
      //     [00] agente   | Buenos dias, le habla Centinela...
      // Antes buscaba el hablante al principio de la linea y no lo encontraba
      // nunca, asi que TODOS los turnos salian marcados como "sistema" y con el
      // prefijo "[00] agente |" dentro del texto. En un registro clinico saber
      // quien dijo cada cosa no es un adorno.
      const FORMATO = /^\[(\d+)\]\s*(\w+)\s*\|\s*(.*)$/;
      texto.split("\n").forEach((linea) => {
        const m = linea.match(FORMATO);
        const quien = m ? m[2].toLowerCase() : "sistema";
        const dicho = m ? m[3] : linea;
        const div = el("div", `turno ${quien}`);
        div.append(el("div", "quien", quien));
        div.append(el("div", null, dicho));
        trans.append(div);
      });
    } else {
      trans.append(el("p", "vacio", "Esta llamada no dejó transcripción."));
    }

    pintarNivel(p.nivel_final || "—");
    $("#motivo-decision").textContent = ticket
      ? ticket.motivo
      : "Sin alerta registrada para esta llamada.";
    $("#motivo-decision").className = "motivo";

    pintarReglasDeTicket(ticket);
    pintarCitasDeTicket(ticket);
    $("#tabla-estado").textContent = "";
    $("#latencia").textContent = "";
    $("#latencia").append(el("p", "vacio", "Solo se mide en vivo."));

    cargarCola();
  }

  function pintarReglasDeTicket(ticket) {
    const cont = $("#reglas-disparadas");
    cont.textContent = "";
    const reglas = (ticket && ticket.reglas) || [];
    if (!reglas.length) {
      cont.append(el("p", "vacio", "Ninguna regla registrada."));
      return;
    }
    reglas.forEach((r) => {
      const div = el("div", `regla ${ticket.nivel === "rojo" ? "roja" : "amarilla"}`);
      div.append(el("div", "codigo", r.codigo || "—"));
      div.append(el("div", null, r.descripcion || ""));
      if (r.valor_observado !== undefined) {
        div.append(el("div", "dato", `observado: ${r.valor_observado} · umbral: ${r.umbral}`));
      }
      if (r.cita && r.cita.documento) {
        div.append(el("div", "cita", `respaldo: ${r.cita.documento}, pág. ${r.cita.pagina ?? "?"}`));
      }
      cont.append(div);
    });
  }

  function pintarCitasDeTicket(ticket) {
    const cont = $("#citas");
    cont.textContent = "";
    const citas = (ticket && ticket.citas) || [];
    if (!citas.length) {
      cont.append(el("p", "vacio", "Sin citas registradas."));
      return;
    }
    citas.forEach((c) => {
      const div = el("div", "cita-doc");
      div.append(el("div", "doc-nombre", c.documento || "?"));
      div.append(el("div", "doc-pagina", `pág. ${c.pagina ?? "?"}`));
      if (c.cita_textual) {
        const q = document.createElement("blockquote");
        q.textContent = `"${c.cita_textual}"`;
        div.append(q);
      }
      cont.append(div);
    });
  }

  /** Vuelve de la lectura a la llamada en curso.
   *
   * `limpiarRegistro` existe por un caso concreto: si llega un turno en vivo
   * mientras alguien consulta una llamada de ayer, hay que salir del modo
   * lectura pero NO se puede tocar el registro -- app.js ya escribio el turno
   * nuevo ahi antes de avisarnos, y limpiarlo lo borraria.
   */
  function salirDeLectura(limpiarRegistro = true) {
    panel.leyendo = null;
    $("#tira-cabecera-rotulo").textContent = "La tira · avance de la llamada";
    $("#tira").removeAttribute("hidden");
    $("#tira-leyenda").hidden = false;
    $("#hoja-traspaso").hidden = true;
    $("#aviso-lectura").hidden = true;
    if (limpiarRegistro) {
      $("#transcripcion").textContent = "";
      $("#transcripcion").append(el("p", "vacio", "La conversación aparecerá acá."));
    }
    pintarNivel(ultimoNivel() || "—");
    pintarTira();
    cargarCola();
  }

  function ultimoNivel() {
    const u = panel.turnos[panel.turnos.length - 1];
    return u ? u.nivel : null;
  }

  function alerta(mensaje) {
    const trans = $("#transcripcion");
    const div = el("div", "turno sistema alerta");
    div.append(el("div", "quien", "sistema"));
    div.append(el("div", null, mensaje));
    trans.append(div);
  }

  /* ========================= consola de pruebas ========================== */

  let sondeo = null;

  async function cargarSuites() {
    const cont = $("#suites");
    if (!cont) return;
    let datos = null;
    try {
      datos = await pedir("/api/pruebas");
    } catch (e) {
      cont.textContent = "";
      cont.append(el("p", "vacio", `No se pudo leer el catálogo: ${e.message}`));
      return;
    }

    cont.textContent = "";
    let algunaCorriendo = false;

    datos.suites.forEach((s) => {
      if (s.estado === "corriendo") algunaCorriendo = true;

      const caja = el("div", "suite");
      caja.dataset.estado = s.estado;
      caja.append(el("div", "titulo", s.titulo));
      caja.append(el("div", "que", s.que));
      const comando = el("div", "comando", s.comando || s.comando_visible);
      comando.title = s.comando || s.comando_visible;
      caja.append(comando);

      const veredicto = el("div", "veredicto");
      veredicto.append(icono(glifoDeEstado(s.estado), "icono"));
      veredicto.append(el("span", null, textoDeEstado(s)));
      caja.append(veredicto);

      const boton = el("button", "boton chico", s.estado === "corriendo" ? "Corriendo…" : "Ejecutar");
      boton.type = "button";
      boton.disabled = s.estado === "corriendo";
      boton.addEventListener("click", async () => {
        boton.disabled = true;
        boton.textContent = "Corriendo…";
        try {
          await pedir(`/api/pruebas/${s.id}`, { method: "POST" });
        } catch (e) {
          $("#salida-pruebas").textContent = `No se pudo lanzar: ${e.message}`;
        }
        mirarSuite(s.id);
        cargarSuites();
      });
      caja.append(boton);

      if (s.salida) {
        const ver = el("button", "boton chico", "Ver salida");
        ver.type = "button";
        ver.addEventListener("click", () => mirarSuite(s.id));
        caja.append(ver);
      }

      cont.append(caja);
    });

    if (algunaCorriendo && !sondeo) {
      // Se sondea mientras haya algo corriendo, y se para en cuanto termina.
      // Un intervalo que sigue vivo para siempre en una pestana abierta es una
      // fuga silenciosa.
      sondeo = setInterval(async () => {
        await cargarSuites();
        if (suiteVigilada) await mirarSuite(suiteVigilada, true);
      }, 2000);
    } else if (!algunaCorriendo && sondeo) {
      clearInterval(sondeo);
      sondeo = null;
    }
  }

  let suiteVigilada = null;

  async function mirarSuite(suiteId, silencioso = false) {
    suiteVigilada = suiteId;
    try {
      const s = await pedir(`/api/pruebas/${suiteId}`);
      const pre = $("#salida-pruebas");
      const alFinal = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 24;
      pre.textContent = s.salida || "(sin salida todavía)";
      if (alFinal || !silencioso) pre.scrollTop = pre.scrollHeight;
    } catch (e) {
      if (!silencioso) $("#salida-pruebas").textContent = e.message;
    }
  }

  function glifoDeEstado(estado) {
    const mapa = {
      ok: "nivel-verde", fallo: "nivel-rojo", error: "nivel-rojo",
      corriendo: "reloj", pendiente: "nivel-sin",
    };
    return mapa[estado] || "nivel-sin";
  }

  function textoDeEstado(s) {
    const segundos = s.ms ? ` · ${(s.ms / 1000).toFixed(1)} s` : "";
    const mapa = {
      ok: `pasa${segundos}`,
      fallo: `falla (salida ${s.codigo_salida})${segundos}`,
      error: `error de ejecución${segundos}`,
      corriendo: "corriendo…",
      pendiente: "sin ejecutar",
    };
    return mapa[s.estado] || s.estado;
  }

  /* =============================== eventos ================================ */

  document.addEventListener("centinela:reinicio", () => {
    panel.turnos = [];
    panel.leyendo = null;
    salirDeLectura();
    pintarNivel("—");
  });

  document.addEventListener("centinela:inicio", (e) => {
    panel.llamadaId = e.detail.llamada_id;
    panel.paciente = e.detail.paciente;
    panel.turnos = [];
    pintarNivel("abierta");
    pintarTira();
    cargarCola();
  });

  document.addEventListener("centinela:turno", (e) => {
    const r = e.detail || {};
    // Un turno en vivo manda sobre la consulta del historial. Sin limpiar el
    // registro: app.js ya escribio este turno ahi.
    if (panel.leyendo) salirDeLectura(false);

    const estadoClinico = r.estado_clinico || {};
    const conocidos = new Set(
      DOMINIOS
        .filter((d) => {
          const o = estadoClinico[d.campo];
          return Boolean(o && o.conocido && o.valor !== null && o.valor !== undefined);
        })
        .map((d) => d.clave),
    );

    panel.turnos.push({
      nivel: r.decision ? r.decision.nivel : ultimoNivel(),
      conocidos,
      estado: estadoClinico,
      dominio: r.dominio_actual || null,
      escala: Boolean(r.escala_ahora),
    });

    pintarTira();
    if (r.decision) pintarNivel(r.decision.nivel);
    cargarCola();
  });

  document.addEventListener("centinela:fin", () => {
    cargarCola();
  });

  // La consola de pruebas se carga al entrar en su pestana, no al arrancar: son
  // subprocesos y no hay razon para tocarlos hasta que alguien los mire.
  const pestanas = $("#pestanas");
  if (pestanas) {
    pestanas.addEventListener("click", (e) => {
      const btn = e.target.closest(".pestana");
      if (btn && btn.dataset.vista === "pruebas") cargarSuites();
    });
  }

  /* =============================== arranque =============================== */

  const btnSalir = $("#btn-salir-lectura");
  if (btnSalir) btnSalir.addEventListener("click", () => salirDeLectura());

  pintarTira();
  pintarNivel("—");
  cargarCola();
  setInterval(() => { if (!panel.leyendo) cargarCola(); }, 15000);
})();
