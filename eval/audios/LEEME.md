# Grabaciones para la prueba de escucha

Aqui van los WAV con voz humana. El guion --que decir y en que archivo-- lo imprime:

```bash
make escucha-guion
```

Y para medir lo que haya grabado:

```bash
make escucha
```

## Por que voz humana y no la sintetica

`eval/probar_ws.py` ya prueba el camino de voz completo, pero el audio lo genera
Piper: es el TTS del sistema hablandole a su propio STT. Eso valida la tuberia
--remuestreo, VAD, WebSocket, latencia-- y no valida la escucha, porque una voz
sintetica es limpia, va a volumen constante y no tiene acento.

De hecho la sintetica es mas dificil en algunos casos y mas facil en otros, asi que
sus numeros no sirven como referencia de lo que va a pasar en la demo.

## Formato

WAV, PCM 16 bits. Cualquier frecuencia de muestreo, mono o estereo: se remuestrea
a 16 kHz con el mismo algoritmo que usa el navegador, para que lo medido aqui se
corresponda con lo que pasa en una llamada de verdad.

Los archivos no se versionan --son voz de una persona-- salvo que se decida lo
contrario. El `.gitignore` de esta carpeta se encarga.
