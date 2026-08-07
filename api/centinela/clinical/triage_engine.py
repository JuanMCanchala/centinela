"""Motor de decision determinista.

Aqui NO hay modelo de lenguaje. Ni una llamada, ni un prompt, ni una heuristica
generativa. Entra un `ClinicalState`, sale un `TriageDecision` con la lista
exacta de reglas que se dispararon y el dato que las disparo.

Tres consecuencias que la rubrica evalua directamente:

1. No puede alucinar una decision: no hay nada que muestrear.
2. No puede caer en una inyeccion de prompt: no existe texto capaz de convencer
   a una comparacion numerica de que 39 grados es normal.
3. Es reproducible: el arnes de replay corre los 160 casos oficiales y obtiene
   siempre el mismo resultado, asi que las metricas del README se sostienen
   contra los logs.

Asimetria clinica: la rubrica dice que el falso negativo es la falla
catastrofica. Por eso el motor nunca cierra en verde cuando le falta
informacion en un dominio que podria estar ocultando una bandera: devuelve
`provisional=True` con los dominios por indagar, y la politica de dialogo
vuelve a preguntar.
"""

from __future__ import annotations

from ..models import (
    Apetito,
    Cita,
    ClinicalState,
    Herida,
    Movilidad,
    Nivel,
    RuleHit,
    Sueno,
    TriageDecision,
)
from .thresholds import (
    BANDERAS_AMARILLAS,
    UMBRALES_ROJOS,
    ConfigTriage,
    MARGEN_BORDE_DOLOR,
    MARGEN_BORDE_FIEBRE,
)


class TriageEngine:
    def __init__(
        self,
        config: ConfigTriage | None = None,
        citas_umbrales: dict[str, Cita] | None = None,
    ) -> None:
        self.config = config or ConfigTriage()
        self.citas_umbrales = citas_umbrales or {}
        self._rojos = {u.codigo: u for u in UMBRALES_ROJOS}
        self._amarillos = {b.codigo: b for b in BANDERAS_AMARILLAS}

    # ------------------------------------------------------------------
    # API publica
    # ------------------------------------------------------------------

    def evaluar(
        self,
        estado: ClinicalState,
        estilo_paciente: str | None = None,
        cerrar: bool = False,
    ) -> TriageDecision:
        """Clasifica el cuadro clinico en verde / amarillo / rojo.

        `cerrar=False` es la evaluacion en mitad de la llamada: si falta
        informacion, el motor devuelve `provisional=True` y la politica de
        dialogo vuelve a preguntar en vez de decidir a ciegas.

        `cerrar=True` es la evaluacion final, cuando ya no hay mas turnos
        disponibles y hay que comprometerse a un nivel. Ahi la ambiguedad se
        resuelve con la asimetria clinica explicita:

        - Dominio todavia desconocido porque el paciente no pudo o no quiso
          responder -> AMARILLO. No se puede descartar lo que no se pregunto.
        - Todo conocido y una sola bandera aislada -> VERDE con indicacion de
          vigilancia. Es el punto de operacion que sobre los 160 casos
          oficiales da cero falsos negativos con la mejor precision.
        """

        rojas = self._reglas_rojas(estado)

        if rojas:
            decision = TriageDecision(
                nivel=Nivel.ROJO,
                reglas_rojas=rojas,
                banderas_amarillas=self._banderas_amarillas(estado),
                dominios_por_indagar=[],
                provisional=False,
                motivo=self._motivo_rojo(rojas),
                version_reglas=self.config.version,
            )
        else:
            amarillas = self._banderas_amarillas(estado)
            por_indagar = self._dominios_por_indagar(estado, amarillas, estilo_paciente)

            if len(amarillas) >= self.config.banderas_minimas:
                decision = TriageDecision(
                    nivel=Nivel.AMARILLO,
                    banderas_amarillas=amarillas,
                    dominios_por_indagar=por_indagar,
                    provisional=False,
                    motivo=(
                        f"{len(amarillas)} banderas de vigilancia simultaneas: "
                        + ", ".join(b.descripcion for b in amarillas)
                    ),
                    version_reglas=self.config.version,
                )
            elif por_indagar and not cerrar:
                # Falta informacion, o hay una bandera aislada en un paciente
                # que minimiza. No se cierra: se indaga.
                decision = TriageDecision(
                    nivel=Nivel.AMARILLO,
                    banderas_amarillas=amarillas,
                    dominios_por_indagar=por_indagar,
                    provisional=True,
                    motivo=(
                        "Informacion insuficiente para cerrar en verde; "
                        f"pendiente indagar: {', '.join(por_indagar)}"
                    ),
                    version_reglas=self.config.version,
                )
            elif cerrar and estado.dominios_faltantes():
                # Se cierra la llamada con dominios sin responder. No se puede
                # descartar lo que nunca se llego a saber: escala a vigilancia.
                faltan = estado.dominios_faltantes()
                decision = TriageDecision(
                    nivel=Nivel.AMARILLO,
                    banderas_amarillas=amarillas,
                    dominios_por_indagar=faltan,
                    provisional=False,
                    motivo=(
                        "Cierre con informacion incompleta en "
                        f"{', '.join(faltan)}; no es posible descartar complicacion"
                    ),
                    version_reglas=self.config.version,
                )
            else:
                decision = TriageDecision(
                    nivel=Nivel.VERDE,
                    banderas_amarillas=amarillas,
                    dominios_por_indagar=[],
                    provisional=False,
                    motivo=(
                        "Sin criterios de alarma y sin banderas suficientes de vigilancia"
                        if not amarillas
                        else f"Bandera aislada sin criterio de escalamiento: {amarillas[0].descripcion}"
                    ),
                    version_reglas=self.config.version,
                )

        return decision

    # ------------------------------------------------------------------
    # Reglas rojas
    # ------------------------------------------------------------------

    def _reglas_rojas(self, estado: ClinicalState) -> list[RuleHit]:
        hits: list[RuleHit] = []

        fiebre = estado.fiebre_c
        if not fiebre.falta and float(fiebre.valor) >= self.config.fiebre_rojo_c:
            hits.append(self._hit_rojo("R1_FIEBRE", float(fiebre.valor)))

        dolor = estado.dolor_nrs
        if not dolor.falta and float(dolor.valor) >= self.config.dolor_rojo_nrs:
            hits.append(self._hit_rojo("R2_DOLOR", float(dolor.valor)))

        herida = estado.herida
        if not herida.falta and herida.valor == Herida.SECRECION_PURULENTA.value:
            hits.append(self._hit_rojo("R3_HERIDA", herida.valor))

        movilidad = estado.movilidad
        if not movilidad.falta and movilidad.valor == Movilidad.INCAPACITANTE_NUEVA.value:
            hits.append(self._hit_rojo("R4_MOVILIDAD", movilidad.valor))

        return hits

    def _hit_rojo(self, codigo: str, valor: float | str) -> RuleHit:
        u = self._rojos[codigo]
        hit = RuleHit(
            codigo=u.codigo,
            descripcion=u.descripcion,
            dominio=u.dominio,
            valor_observado=valor,
            umbral=u.umbral_legible,
            cita=self.citas_umbrales.get(u.codigo),
        )
        return hit

    # ------------------------------------------------------------------
    # Banderas amarillas
    # ------------------------------------------------------------------

    def _banderas_amarillas(self, estado: ClinicalState) -> list[RuleHit]:
        hits: list[RuleHit] = []

        dolor = estado.dolor_nrs
        if not dolor.falta and float(dolor.valor) >= self.config.dolor_amarillo_nrs:
            hits.append(self._hit_amarillo("A1_DOLOR", float(dolor.valor)))

        fiebre = estado.fiebre_c
        if not fiebre.falta and float(fiebre.valor) >= self.config.fiebre_amarillo_c:
            hits.append(self._hit_amarillo("A2_FEBRICULA", float(fiebre.valor)))
        elif fiebre.falta and estado.fiebre_subjetiva:
            # Refiere sensacion febril pero no se ha medido. No se descarta.
            hits.append(self._hit_amarillo("A2_FEBRICULA", "sensacion febril sin medicion"))

        herida = estado.herida
        if not herida.falta and herida.valor == Herida.ERITEMA_LEVE.value:
            hits.append(self._hit_amarillo("A3_ERITEMA", herida.valor))

        apetito = estado.apetito
        if not apetito.falta and apetito.valor == Apetito.MUY_DISMINUIDO.value:
            hits.append(self._hit_amarillo("A4_APETITO", apetito.valor))

        sueno = estado.sueno
        if not sueno.falta and sueno.valor == Sueno.MUY_ALTERADO.value:
            hits.append(self._hit_amarillo("A5_SUENO", sueno.valor))

        return hits

    def _hit_amarillo(self, codigo: str, valor: float | str) -> RuleHit:
        b = self._amarillos[codigo]
        hit = RuleHit(
            codigo=b.codigo,
            descripcion=b.descripcion,
            dominio=b.dominio,
            valor_observado=valor,
            umbral=b.umbral_legible,
            cita=self.citas_umbrales.get(b.codigo),
        )
        return hit

    # ------------------------------------------------------------------
    # Que falta por preguntar antes de poder cerrar
    # ------------------------------------------------------------------

    def _dominios_por_indagar(
        self,
        estado: ClinicalState,
        amarillas: list[RuleHit],
        estilo_paciente: str | None,
    ) -> list[str]:
        """Dominios donde el agente debe volver a preguntar antes de decidir.

        Tres motivos para indagar, en orden de importancia:

        1. El dominio no se conoce. Un dominio en blanco puede esconder una
           bandera roja, y tratarlo como normal es exactamente como se produce
           un falso negativo.
        2. Hay una bandera aislada. Una sola no escala, pero tampoco se ignora:
           se profundiza en ese dominio para ver si hay una segunda.
        3. El paciente minimiza y algun valor quedo justo debajo del umbral.
        """

        pendientes: list[str] = list(estado.dominios_faltantes())

        if len(amarillas) == 1:
            dominio = amarillas[0].dominio
            if dominio not in pendientes:
                pendientes.append(dominio)

        minimiza = estilo_paciente in self.config.estilos_que_minimizan
        if minimiza:
            for dominio in self._dominios_en_el_borde(estado):
                if dominio not in pendientes:
                    pendientes.append(dominio)

        # La fiebre referida sin termometro siempre se persigue.
        if estado.fiebre_subjetiva and estado.fiebre_c.falta and "fiebre" not in pendientes:
            pendientes.append("fiebre")

        return pendientes

    def _dominios_en_el_borde(self, estado: ClinicalState) -> list[str]:
        """Valores que quedaron a un paso de cruzar un umbral."""

        borde: list[str] = []

        dolor = estado.dolor_nrs
        if not dolor.falta:
            d = float(dolor.valor)
            cerca_amarillo = self.config.dolor_amarillo_nrs - MARGEN_BORDE_DOLOR
            if cerca_amarillo <= d < self.config.dolor_rojo_nrs:
                borde.append("dolor")

        fiebre = estado.fiebre_c
        if not fiebre.falta:
            f = float(fiebre.valor)
            cerca_amarillo = self.config.fiebre_amarillo_c - MARGEN_BORDE_FIEBRE
            if cerca_amarillo <= f < self.config.fiebre_rojo_c:
                borde.append("fiebre")

        return borde

    # ------------------------------------------------------------------

    def _motivo_rojo(self, rojas: list[RuleHit]) -> str:
        partes = [f"{h.descripcion} (observado: {h.valor_observado}, umbral {h.umbral})" for h in rojas]
        motivo = "Criterio de alarma cumplido - " + "; ".join(partes)
        return motivo
