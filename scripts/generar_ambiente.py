"""Genera el audio de ambiente de fondo de la llamada.

Por que existe: el silencio digital absoluto es lo que delata a un agente. Una
llamada humana desde un centro de seguimiento trae de fondo teclado, murmullo
lejano, el zumbido de la linea. Sin nada de eso, el paciente percibe un vacio
antinatural entre frases, y ese vacio hace mas por revelar que es una maquina que
cualquier detalle de la voz.

Se genera por sintesis en vez de descargar un archivo. Tres razones:

- Sin dependencias ni descargas: importa para la compuerta de 15 minutos.
- Sin problemas de licencia de un audio ajeno.
- Se puede hacer que empalme consigo mismo sin costura -- una grabacion cualquiera
  produce un chasquido audible cada vez que el bucle vuelve a empezar, y ese
  chasquido periodico delata la maquina mas que el silencio.

Capas del resultado:
  1. Zumbido de linea telefonica: ruido rosa muy suave, filtrado a banda de voz.
  2. Teclado: transitorios cortos con decaimiento rapido, a ritmo irregular.
  3. Murmullo lejano: ruido de banda estrecha con envolvente lenta, como una
     conversacion en otra mesa de la que no se distinguen palabras.

    python scripts/generar_ambiente.py
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
DESTINO = RAIZ / "web" / "ambiente.wav"

FRECUENCIA = 22050
SEGUNDOS = 24.0          # bucle largo: uno corto se reconoce y suena repetitivo
NIVEL_LINEA = 0.0075     # el zumbido tiene que estar al borde de lo audible
NIVEL_TECLADO = 0.055
NIVEL_MURMULLO = 0.014

SEMILLA = 20260807


class Aleatorio:
    """Generador congruencial propio, para no depender de numpy en este script."""

    def __init__(self, semilla: int) -> None:
        self.estado = semilla & 0xFFFFFFFF

    def siguiente(self) -> float:
        self.estado = (1103515245 * self.estado + 12345) & 0x7FFFFFFF
        return self.estado / 0x7FFFFFFF

    def entre(self, a: float, b: float) -> float:
        return a + (b - a) * self.siguiente()

    def normal(self) -> float:
        # Suma de uniformes: suficiente para ruido y mas barato que Box-Muller.
        return sum(self.siguiente() for _ in range(4)) / 2 - 1


def envolvente_bucle(i: int, n: int) -> float:
    """Ventana que garantiza empalme sin costura.

    El primer y el ultimo cuarto de segundo se cruzan en amplitud, asi que el
    final del bucle encaja con su principio y no hay chasquido periodico.
    """

    cruce = int(FRECUENCIA * 0.25)
    if i < cruce:
        factor = i / cruce
    elif i > n - cruce:
        factor = (n - i) / cruce
    else:
        factor = 1.0
    return factor


def generar() -> list[float]:
    n = int(SEGUNDOS * FRECUENCIA)
    r = Aleatorio(SEMILLA)
    salida = [0.0] * n

    # ---------------- capa 1: zumbido de linea ----------------
    # Ruido rosa aproximado por promedio movil de ruido blanco, que atenua agudos
    # y deja ese "hiss" grave de una linea abierta.
    previo = 0.0
    for i in range(n):
        blanco = r.normal()
        previo = previo * 0.94 + blanco * 0.06
        salida[i] += previo * NIVEL_LINEA * 12

    # ---------------- capa 2: teclado ----------------
    # Un teclazo es un transitorio de pocos milisegundos con decaimiento casi
    # instantaneo. El ritmo va en rachas: una persona escribe varias teclas
    # seguidas y luego para, no a intervalos regulares.
    i = int(r.entre(0, FRECUENCIA * 2))
    while i < n:
        teclas_en_racha = int(r.entre(3, 14))
        for _ in range(teclas_en_racha):
            if i >= n:
                break
            largo = int(r.entre(0.004, 0.011) * FRECUENCIA)
            amplitud = NIVEL_TECLADO * r.entre(0.45, 1.0)
            # Dos teclas nunca suenan igual: se varia el brillo del transitorio.
            brillo = r.entre(0.25, 0.85)
            fase = 0.0
            for j in range(largo):
                if i + j >= n:
                    break
                decaimiento = math.exp(-j / (largo * 0.28))
                fase = fase * (1 - brillo) + r.normal() * brillo
                salida[i + j] += fase * amplitud * decaimiento
            # Separacion entre teclas dentro de la racha.
            i += int(r.entre(0.07, 0.22) * FRECUENCIA)
        # Pausa entre rachas.
        i += int(r.entre(1.2, 5.0) * FRECUENCIA)

    # ---------------- capa 3: murmullo lejano ----------------
    # Ruido de banda estrecha con envolvente silabica lenta: se percibe como
    # alguien hablando lejos sin que se entienda una palabra, que es exactamente
    # lo que se oye en una oficina.
    i = int(r.entre(0, FRECUENCIA * 4))
    while i < n:
        largo = int(r.entre(1.5, 4.5) * FRECUENCIA)
        centro = r.entre(180, 420)     # zona grave: la distancia se come agudos
        nivel = NIVEL_MURMULLO * r.entre(0.5, 1.0)
        b1 = b2 = 0.0
        for j in range(largo):
            if i + j >= n:
                break
            t = j / FRECUENCIA
            # Envolvente de silabas, ~4 por segundo, mas una envolvente de frase.
            silaba = 0.55 + 0.45 * math.sin(2 * math.pi * 4.1 * t)
            frase = math.sin(math.pi * j / largo) ** 1.5
            blanco = r.normal()
            # Resonador de dos polos alrededor de `centro`.
            w = 2 * math.pi * centro / FRECUENCIA
            b1, b2 = (blanco + 1.96 * math.cos(w) * b1 - 0.962 * b2), b1
            salida[i + j] += b1 * 0.02 * nivel * silaba * frase
        i += largo + int(r.entre(2.0, 7.0) * FRECUENCIA)

    # ---------------- empalme y normalizacion ----------------
    for i in range(n):
        salida[i] *= envolvente_bucle(i, n)

    pico = max(abs(v) for v in salida) or 1.0
    # Se deja con mucho margen: el ambiente tiene que estar por debajo de la voz,
    # no competir con ella. Un fondo audible es peor que no tener fondo.
    objetivo = 0.16
    for i in range(n):
        salida[i] = salida[i] / pico * objetivo

    return salida


def main() -> int:
    print(f"generando {SEGUNDOS:.0f}s de ambiente a {FRECUENCIA} Hz...")
    muestras = generar()

    DESTINO.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(DESTINO), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(FRECUENCIA)
        w.writeframes(struct.pack(
            f"<{len(muestras)}h",
            *[int(max(-1.0, min(1.0, v)) * 32000) for v in muestras],
        ))

    kb = DESTINO.stat().st_size / 1024
    print(f"escrito {DESTINO.relative_to(RAIZ)}  ({kb:.0f} KB)")
    print("El bucle empalma consigo mismo: no hay chasquido al repetir.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
