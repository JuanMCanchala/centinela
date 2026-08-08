"""Genera el A/B de voz para decidir por oido lo que no se puede decidir midiendo.

Casi todo lo que mejora la voz de este proyecto se puede medir: el spread de volumen
entre locuciones, la variacion de la cola de silencio, si el fonemizador pone la tonica
donde va. Dos cosas no:

  1. **La velocidad.** Mas pausado es mas inteligible y tambien mas lento. Donde esta
     el punto no lo dice ninguna metrica de la rubrica.
  2. **El modelo de voz.** `es_MX-claude-high` cuesta un 14 % mas de RTF que la
     `medium` que usamos (0.403 contra 0.352, `docs/metrics/bench_voces.json`) y el 89 %
     de los turnos sirven audio de cache, asi que el RTF casi no pesa. Si suena mejor,
     el cambio es casi gratis. Si suena igual, no hay razon para tocarlo.

Asi que aqui no se decide nada: se generan las muestras y se escuchan.

La pareja que mas importa es `1_antes` contra `2_despues`. Es el mismo texto con la
ortografia que tenia el guion y sin tratamiento de audio, contra el que tiene ahora.

    python scripts/muestras_prosodia.py
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import wave
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))

from centinela.dialog import script as S  # noqa: E402
from centinela.tts import piper as P  # noqa: E402

SALIDA = RAIZ / "data" / "muestras_prosodia"

# El cierre rojo, que es donde estaban los tres errores de pronunciacion, y una
# pregunta de sueño, que es el dominio donde faltaba la ñ en las seis locuciones.
DESPUES = S.CIERRE_ROJO.texto
ANTES = (
    "Lo que me acaba de describir necesita atencion medica ahora, no manana. "
    "Ya deje una alerta al equipo clinico con sus datos y lo que me conto. "
    "Por favor no espere a que lo llamen: dirijase al servicio de urgencias mas cercano "
    "o llame al 123 si no tiene como desplazarse."
)
SUENO_ANTES = "Sobre el sueno: ha podido dormir en estas noches?"
SUENO_DESPUES = S.PREGUNTA_POR_DOMINIO["sueno"].reintento.texto


def _sintetizar(texto: str, voz: Path, destino: Path, velocidad: float,
                noise_w: float | None = None, tratar: bool = True) -> float:
    """Una invocacion directa del binario. Devuelve la duracion en segundos."""

    crudo = destino.with_suffix(".crudo.wav")
    orden = [
        str(P.PiperTTS._localizar_binario()),
        "--model", str(voz),
        "--length_scale", str(velocidad),
        "--output_file", str(crudo),
        "--quiet",
    ]
    if noise_w is not None:
        orden.extend(["--noise_w", str(noise_w)])

    # `para_voz` solo se aplica al lado "despues": es parte de lo que se compara.
    hablado = P.para_voz(texto) if tratar else texto
    subprocess.run(orden, input=hablado.encode("utf-8") + b"\n", check=True,
                   cwd=str(P.PiperTTS._localizar_binario().parent))

    datos = crudo.read_bytes()
    destino.write_bytes(P.tratar(datos) if tratar else datos)
    crudo.unlink(missing_ok=True)

    with wave.open(str(destino), "rb") as w:
        return w.getnframes() / w.getframerate()


async def main() -> int:
    SALIDA.mkdir(parents=True, exist_ok=True)
    voces = P.DIR_PIPER / "voces"
    actual = voces / "es_MX-ald-medium.onnx"
    alterna = voces / "es_MX-claude-high.onnx"

    if not actual.exists():
        print(f"falta la voz en {voces}: corre `make piper`")
        salida = 1
    else:
        pruebas = [
            # (archivo, texto, voz, velocidad, noise_w, tratar)
            ("1_antes__sin_tildes_sin_tratamiento", ANTES, actual, 1.0, None, False),
            ("2_despues__como_habla_ahora", DESPUES, actual, P.LENGTH_SCALE, None, True),
            ("3_despues__perfil_de_enfasis", DESPUES, actual,
             P.LENGTH_SCALE_ENFASIS, P.NOISE_W_ENFASIS, True),
            ("4_velocidad_1.00", DESPUES, actual, 1.00, None, True),
            ("5_velocidad_1.12", DESPUES, actual, 1.12, None, True),
            ("6_sueno_antes", SUENO_ANTES, actual, 1.0, None, False),
            ("7_sueno_despues", SUENO_DESPUES, actual, P.LENGTH_SCALE, None, True),
        ]

        if alterna.exists():
            pruebas.append(
                ("8_voz_alterna_claude_high", DESPUES, alterna, P.LENGTH_SCALE, None, True)
            )

        print(f"escribiendo en {SALIDA.relative_to(RAIZ)}\n")
        for nombre, texto, voz, velocidad, noise_w, tratar in pruebas:
            destino = SALIDA / f"{nombre}.wav"
            segundos = _sintetizar(texto, voz, destino, velocidad, noise_w, tratar)
            print(f"  {nombre:38} {segundos:5.2f} s   {voz.stem}  x{velocidad}")

        print(
            "\nLa pareja que importa es 1_antes contra 2_despues: el mismo texto con la\n"
            "ortografia que tenia el guion y sin tratamiento, contra el de ahora.\n"
            "En 1_antes se oye 'atencion medica' con la tonica corrida, 'manana' sin ñ y\n"
            "la linea de emergencias dicha como 'ciento veintitres'.\n"
            "En 6 y 7, la palabra 'sueño' -- que se decia 'sueno'."
        )
        salida = 0

    return salida


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
