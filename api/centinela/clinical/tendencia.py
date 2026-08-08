"""Tendencia entre llamadas: lo que dice la medicion y lo que se decidio con ella.

El dataset del reto trae cuatro llamadas por paciente -- dias 1, 3, 7 y 14 -- y cada
una se evaluaba aislada. La idea obvia es que un paciente cuyo dolor va 2 -> 3 -> 5 no
cruza ningun umbral pero su tendencia es informativa, y por eso el informe la tenia
apuntada como la mejora de mayor valor pendiente.

Se midio antes de construirla, y la medicion dijo otra cosa. Barrido de umbrales de
delta sobre las 40 trayectorias oficiales (reproducible con `make tendencia`):

    dolor >= +2 - fiebre >= +0.5   ->  0 avisos anticipados, 17 falsas alarmas
    dolor >= +3 - fiebre >= +0.8   ->  0 avisos anticipados,  5 falsas alarmas
    dolor >= +4 - fiebre >= +1.0   ->  0 avisos anticipados,  0 falsas alarmas

Cero avisos anticipados en las tres filas. Las tres trayectorias que saltan de verde a
rojo van planas antes del salto (dolor 4, 4 -> 9; fiebre 37.4, 37.4 -> 37.9): **el
deterioro del dataset es un escalon, no una rampa, y por deltas no hay nada que
anticipar.** Es un defecto del material entregado, medido y reproducible.

Asi que el valor del historial no esta en una regla que adivine. Esta en dos cosas que
no cuestan precision, y las dos estan en `escalation/service.py`:

  - la serie de dias anteriores en la hoja de traspaso, para el humano que recibe la
    alerta -- un 9 no dice lo mismo si venia de un 4 que si venia de un 8;
  - la reconciliacion: un rojo del dia 7 sin acuse cuando llega la llamada del dia 14.

Las dos reglas de este modulo se mantienen igual, en el punto de operacion de la
tercera fila: **sobre los datos oficiales no disparan ni una vez**, y por eso `make
eval` no se mueve. Se mantienen porque un deterioro real es una rampa y el coste medido
en precision es exactamente cero. `tests/test_tendencia.py` prueba con una rampa
sintetica que disparan cuando la hay, para que no sean una regla decorativa.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Cita, ClinicalState, RuleHit

# Puntos de operacion elegidos midiendo, no a ojo: son los que dan cero falsas alarmas
# sobre las 40 trayectorias oficiales. Ver el barrido en `eval/tendencia.py`.
SALTO_DOLOR_NRS = 4
SALTO_FIEBRE_C = 1.0


@dataclass(frozen=True)
class ReglaTendencia:
    codigo: str
    dominio: str
    descripcion: str
    umbral_legible: str


TENDENCIAS: tuple[ReglaTendencia, ...] = (
    ReglaTendencia(
        codigo="T1_DOLOR_ASCENDENTE",
        dominio="dolor",
        descripcion="El dolor subio de forma marcada respecto a la llamada anterior",
        umbral_legible=f"salto >= +{SALTO_DOLOR_NRS} puntos NRS entre llamadas",
    ),
    ReglaTendencia(
        codigo="T2_FIEBRE_ASCENDENTE",
        dominio="fiebre",
        descripcion="La temperatura subio de forma marcada respecto a la llamada anterior",
        umbral_legible=f"salto >= +{SALTO_FIEBRE_C} C entre llamadas",
    ),
)

_POR_CODIGO = {t.codigo: t for t in TENDENCIAS}

# Los dos dominios numericos. Los cualitativos no se comparan por delta: el paso de
# `normal` a `eritema_leve` ya lo recoge `A3_ERITEMA` como valor absoluto, y anadir la
# misma senal por dos caminos solo duplica la bandera.
CAMPO = {"dolor": "dolor_nrs", "fiebre": "fiebre_c"}


def _ultimo_valor(serie: dict, dominio: str) -> tuple[int, float] | None:
    """El valor mas reciente de este dominio en las llamadas anteriores.

    `serie` es lo que devuelve `EscalationService.serie_por_dominio`: dominio ->
    [(dia, valor como texto), ...] en orden de dia. El valor viene como texto porque la
    tabla guarda los seis dominios en una columna, y aqui solo interesan los dos
    numericos.
    """

    encontrado: tuple[int, float] | None = None
    for dia, bruto in serie.get(dominio) or []:
        try:
            valor = float(bruto)
        except (TypeError, ValueError):
            valor = None
        if valor is not None:
            encontrado = (dia, valor)
    return encontrado


def banderas_de_tendencia(
    estado: ClinicalState,
    serie: dict | None,
    citas: dict[str, Cita] | None = None,
) -> list[RuleHit]:
    """Banderas amarillas por salto respecto a la llamada anterior.

    Sin serie devuelve lista vacia, asi que una llamada sin historia -- la primera de
    un paciente, o cualquier evaluacion offline -- se comporta exactamente igual que
    antes de que este modulo existiera.
    """

    hits: list[RuleHit] = []

    if serie:
        for dominio, minimo in (("dolor", SALTO_DOLOR_NRS), ("fiebre", SALTO_FIEBRE_C)):
            obs = estado.observacion(dominio)
            anterior = _ultimo_valor(serie, dominio)
            if not obs.falta and anterior is not None:
                dia, antes = anterior
                ahora = float(obs.valor)
                if ahora - antes >= minimo:
                    regla = _POR_CODIGO[
                        "T1_DOLOR_ASCENDENTE" if dominio == "dolor" else "T2_FIEBRE_ASCENDENTE"
                    ]
                    hits.append(
                        RuleHit(
                            codigo=regla.codigo,
                            descripcion=regla.descripcion,
                            dominio=regla.dominio,
                            valor_observado=f"dia {dia}: {antes} -> hoy: {ahora}",
                            umbral=regla.umbral_legible,
                            cita=(citas or {}).get(regla.codigo),
                        )
                    )

    return hits
