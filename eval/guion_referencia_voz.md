# Guion para grabar la voz de referencia

El modelo de clonación no copia solo el **timbre**: copia el **estilo**. Si la grabación de
referencia suena a alguien leyendo un periódico, el agente sonará a alguien leyendo un
periódico. Así que este guion está escrito para que la referencia traiga exactamente lo que
el agente necesita saber hacer: saludar con calma, preguntar con entonación de pregunta, dar
una instrucción clara, y decir números.

## Cómo grabar

| | |
|---|---|
| Duración | **30-45 segundos.** Menos de 20 s clona peor; más de un minuto no aporta |
| Formato | **WAV, mono.** A 48 kHz o 44.1 kHz. Si solo puedes MP3, sirve, pero a 192 kbps o más |
| Sala | Silenciosa. Sin música, sin televisión, sin ventilador, sin eco de baño |
| Micrófono | A un palmo de la boca. Los audífonos del celular sirven; el manos libres del carro no |
| Procesado | **Ninguno.** Sin reverb, sin ecualizador, sin reducción de ruido, sin normalizar |
| Voz | Normal. Sin susurrar, sin proyectar como en un escenario, sin sonreír al hablar |

Lo que más ayuda: **lee despacio y con calma, como si al otro lado hubiera alguien recién
operado que te está escuchando.** Ese es el registro que queremos que herede.

Si te trabas, no empieces de nuevo: sigue y graba otra toma completa. Es mejor tener dos
tomas enteras que una pegada de trozos.

## Qué decir

> Buenos días, señora. Le habla Centinela, del equipo de seguimiento de su cirugía. La llamo
> para saber cómo se ha sentido estos días en casa, con tranquilidad, sin apuro.
>
> ¿Cómo ha estado el dolor? Si cero es nada y diez es lo más fuerte que ha sentido, ¿en qué
> número lo pondría hoy?
>
> ¿Y ha tenido fiebre? Si se tomó la temperatura y le marcó treinta y ocho y medio, dígamelo
> con confianza.
>
> Ahora mire la herida un momento, por favor. Recuerde revisarla también mañana. Si la ve
> enrojecida, hinchada, o si le sale algún líquido amarillo, necesito saberlo enseguida.
>
> Yo voy anotando todo y el equipo clínico lo revisa. Guarde reposo, cuídese mucho, y que
> siga mejorando.

## Por qué esas frases y no otras

No es un texto cualquiera. Cubre lo que la voz clonada tiene que poder hacer:

- **Tres preguntas** con entonación ascendente. Sin ellas, el clon lee las preguntas del
  guion como si fueran afirmaciones, y eso es lo primero que suena a máquina.
- **Números dichos en palabras** —cero, diez, treinta y ocho y medio— porque el agente los
  dice todo el tiempo y son donde más se nota una prosodia mal aprendida.
- **Las palabras del dominio**: herida, fiebre, temperatura, enrojecida, hinchada, líquido
  amarillo, cirugía, reposo. El clon las va a decir cientos de veces.
- **Cobertura fonética del español**: la ñ (señora, mañana), la rr (recuerde, enrojecida),
  la ll y la y (llama, amarillo, ya), la j (cirugía), la d entre vocales (cuídese, dígamelo),
  y las cinco vocales tónicas y átonas.
- **Una instrucción y un cierre cálido**, que son los dos registros del final de la llamada.

## Dónde dejar el archivo

En cualquier carpeta; dime la ruta. **No se versiona en el repositorio**: es la voz de una
persona real y lo que se publica es el audio sintetizado, no su muestra. Lo que sí queda
declarado en el informe final es que la voz del agente es una clonación con consentimiento.
