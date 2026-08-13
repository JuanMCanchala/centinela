# Ensayo cronometrado de la compuerta G2 del reto.
#
# G2 dice: "Siguiendo unicamente tu README -- credenciales, URLs y accesos
# incluidos -- la solucion queda corriendo y accesible en 15 minutos o menos".
# Y fallarla significa que la entrega NO SE PUNTUA.
#
# Este script ejecuta exactamente los pasos del README, desde un clon limpio, y
# cronometra cada uno. Lo que mide no es "cuanto tarda en mi maquina ya
# preparada": clona el repo en un directorio nuevo, con cache de modelos vacia si
# se le pide, para medir lo que el jurado va a vivir.
#
#   .\scripts\ensayo_g2.ps1                    # reutiliza los modelos ya bajados
#   .\scripts\ensayo_g2.ps1 -SinCache          # simula una maquina virgen
#   .\scripts\ensayo_g2.ps1 -Destino D:\tmp\g2
#
# Sale con codigo distinto de cero si pasa de 15 minutos o si el sistema no
# responde al final.

param(
    [string]$Destino = "",
    [string]$Repo = "https://github.com/JuanMCanchala/centinela.git",
    [switch]$SinCache,
    [int]$Puerto = 8477
)

$ErrorActionPreference = "Continue"
$LIMITE_MINUTOS = 15

if (-not $Destino) {
    $Destino = Join-Path $env:TEMP ("g2_" + (Get-Date -Format "yyyyMMdd_HHmmss"))
}

$pasos = @()
$reloj = [System.Diagnostics.Stopwatch]::StartNew()

function Paso {
    param([string]$Nombre, [scriptblock]$Accion)
    $t = [System.Diagnostics.Stopwatch]::StartNew()
    Write-Host ""
    Write-Host ("=" * 74)
    Write-Host ("  {0}   [transcurrido {1:mm\:ss}]" -f $Nombre, $reloj.Elapsed)
    Write-Host ("=" * 74)
    $ok = $true
    try { & $Accion } catch { $ok = $false; Write-Host "  ERROR: $_" }
    $t.Stop()
    $script:pasos += [pscustomobject]@{
        Paso = $Nombre; Segundos = [math]::Round($t.Elapsed.TotalSeconds, 1); OK = $ok
    }
    Write-Host ("  -> {0:N1}s" -f $t.Elapsed.TotalSeconds)
}

Write-Host ""
Write-Host "ENSAYO DE LA COMPUERTA G2"
Write-Host "  destino     : $Destino"
Write-Host "  repo        : $Repo"
Write-Host "  puerto      : $Puerto"
Write-Host "  cache modelos: $(if ($SinCache) { 'VACIA (maquina virgen)' } else { 'reutilizada' })"
Write-Host "  limite      : $LIMITE_MINUTOS minutos"

Paso "1. Clonar el repositorio" {
    if (Test-Path $Destino) { Remove-Item -Recurse -Force $Destino }
    git clone --depth 1 $Repo $Destino 2>&1 | Out-Null
    if (-not (Test-Path (Join-Path $Destino "README.md"))) { throw "el clon no trajo el README" }
}

Paso "2. make instalar (entorno + dependencias)" {
    Push-Location $Destino
    uv venv --python 3.11 2>&1 | Out-Null
    uv pip install -r pyproject.toml 2>&1 | Select-Object -Last 2
    Pop-Location
}

Paso "3. make ollama (modelo de lenguaje)" {
    $ol = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
    if (-not (Test-Path $ol)) {
        throw "Ollama no esta instalado. El README lo declara como requisito previo."
    }
    $ya = (& $ol list 2>&1 | Out-String)
    if ($ya -match "phi3\.5:3\.8b-mini-instruct-q4_K_M") {
        Write-Host "  modelo ya presente (2.4 GB); no se re-descarga"
        $script:modeloYaEstaba = $true
    } else {
        $salida = (& $ol pull phi3.5:3.8b-mini-instruct-q4_K_M 2>&1 | Out-String)
        # Un pull fallido no puede reportarse como paso exitoso: es justo el tipo
        # de error que hace que la compuerta G2 se falle en la maquina del jurado
        # mientras el ensayo decia que todo iba bien.
        if ($salida -match "Error|error:") {
            throw "el pull del modelo fallo: $($salida.Trim() -split "`n" | Select-Object -Last 1)"
        }
        Write-Host "  modelo descargado"
    }
}

Paso "4. make piper (voz)" {
    Push-Location $Destino
    if (-not $SinCache -and (Test-Path "$PSScriptRoot\..\data\piper")) {
        # Se copia en vez de re-descargar 427 MB: mide el caso realista de un
        # jurado con la descarga ya hecha. Con -SinCache se baja de verdad.
        Copy-Item -Recurse "$PSScriptRoot\..\data\piper" "$Destino\data\piper"
        Write-Host "  reutilizando binario y voces ya descargados"
    } else {
        .venv\Scripts\python.exe scripts\fetch_piper.py 2>&1 | Select-Object -Last 2
    }
    Pop-Location
}

Paso "5. make modelos (embeddings + STT)" {
    Push-Location $Destino
    if (-not $SinCache -and (Test-Path "$PSScriptRoot\..\data\modelos")) {
        Copy-Item -Recurse "$PSScriptRoot\..\data\modelos" "$Destino\data\modelos"
        Write-Host "  reutilizando modelos ya descargados"
    } else {
        $env:FASTEMBED_CACHE_PATH = "$Destino\data\modelos"
        .venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'api'); from centinela.rag.embedder import Embedder; e=Embedder(); e.calentar(); print('embeddings OK')" 2>&1 | Select-Object -Last 1
        .venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'api'); from pathlib import Path; from centinela.stt.whisper import WhisperSTT; s=WhisperSTT(dir_modelos=Path('data/modelos/whisper')); s.calentar(); print('STT OK')" 2>&1 | Select-Object -Last 1
    }
    Pop-Location
}

Paso "6. Extraer el indice del corpus" {
    Push-Location $Destino
    .venv\Scripts\python.exe scripts\empacar_index.py extraer 2>&1 | Select-Object -Last 2
    Pop-Location
}

$proceso = $null
Paso "7. make up (arranque) y primera respuesta" {
    Push-Location $Destino
    $script:proceso = Start-Process -PassThru -WindowStyle Hidden `
        -FilePath "$Destino\.venv\Scripts\python.exe" `
        -ArgumentList "-m","uvicorn","centinela.main:app","--app-dir","api","--host","127.0.0.1","--port","$Puerto"
    Pop-Location

    $vivo = $false
    for ($i = 0; $i -lt 150; $i++) {
        try {
            $s = Invoke-RestMethod "http://127.0.0.1:$Puerto/api/salud" -TimeoutSec 4
            if ($s.ok) {
                $vivo = $true
                Write-Host ("  sistema arriba: {0} docs, {1} fragmentos, modelo {2}" -f `
                    $s.corpus.documentos, $s.corpus.chunks, $s.llm.modelo_configurado)
                Write-Host ("  voz: {0} / {1} locuciones en cache" -f $s.tts.voz, $s.tts.locuciones_en_cache)
                Write-Host ("  voz clonada: {0} locuciones, disponible={1}" -f `
                    $s.tts.voz_clonada.locuciones, $s.tts.voz_clonada.disponible)
                break
            }
        } catch { Start-Sleep -Milliseconds 2000 }
    }
    if (-not $vivo) { throw "la API no respondio tras el arranque" }
}

Paso "8. Verificacion funcional minima" {
    # Las cuatro cosas que el jurado va a probar de entrada.
    $b = '{"paciente_id":"g2","nombre":"Ensayo G2","procedimiento":"Apendicectomia","dia_postop":7,"edad":40,"genero":"M","comorbilidades":[]}'
    $ini = Invoke-RestMethod "http://127.0.0.1:$Puerto/api/llamadas" -Method Post -Body $b -ContentType "application/json"
    Write-Host "  llamada iniciada, el agente habla: $($ini.agente_dice.Substring(0,60))..."
    Write-Host "  audio generado: $($ini.audio_bytes) bytes"

    $t = '{"texto":"La herida tiene un liquido amarillo saliendo"}'
    $r = Invoke-RestMethod "http://127.0.0.1:$Puerto/api/llamadas/$($ini.llamada_id)/turno" -Method Post -Body $t -ContentType "application/json"
    Write-Host "  turno de bandera roja -> nivel=$($r.decision.nivel)  escala=$($r.escala_ahora)"
    if ($r.decision.nivel -ne "rojo") { throw "no escalo un caso rojo evidente" }

    $docs = Invoke-RestMethod "http://127.0.0.1:$Puerto/api/documentos"
    Write-Host "  consola: $($docs.total) documentos indexados, generacion $($docs.generacion)"
    if ($docs.total -lt 100) { throw "el indice no se extrajo completo" }

    # La voz del agente son WAV versionados, no un modelo que se instale: el que genera la
    # clonacion arrastra 2.5 GB de torch y no cabe en esta compuerta. Por eso el riesgo aqui
    # no es que falle una descarga, es que un `.gitignore` los excluya y el clon del jurado
    # se quede hablando con la voz de respaldo sin que nada lo avise. Se comprueba en el clon.
    $clonados = @(Get-ChildItem (Join-Path $Destino "data\audio_clon") -Filter *.wav -ErrorAction SilentlyContinue)
    Write-Host "  voz clonada en el clon: $($clonados.Count) WAV"
    if ($clonados.Count -lt 250) {
        throw "el clon no trajo la voz clonada ($($clonados.Count) WAV): el agente hablaria con el respaldo"
    }
}

$reloj.Stop()
if ($proceso) { Stop-Process -Id $proceso.Id -Force -ErrorAction SilentlyContinue }

Write-Host ""
Write-Host ("=" * 74)
Write-Host "  RESULTADO DEL ENSAYO G2"
Write-Host ("=" * 74)
$pasos | Format-Table -AutoSize
$total = $reloj.Elapsed.TotalMinutes
Write-Host ("  Medido: {0:N1} minutos   (limite {1})" -f $total, $LIMITE_MINUTOS)

# El tiempo medido no es el tiempo del jurado si se reutilizaron descargas. Se
# suma una estimacion honesta de lo que costaria bajarlas, para que el numero que
# se reporta en el README no sea el mejor caso.
$descargasPendientes = 0
$notas = @()

# La descarga del REPOSITORIO tambien cuenta, y el ensayo no la mide: clonar de una ruta
# local tarda segundos, y clonar del remoto depende de la red del jurado y no de la mia. El
# repositorio versiona ~100 MB a proposito --el indice del corpus comprimido, el cache de
# audio y las 300 locuciones de la voz clonada-- porque reconstruir cualquiera de las tres
# cosas no cabe en quince minutos. Ese peso es una decision, asi que su costo se reporta.
# Se suman los archivos que git TIENE VERSIONADOS, que es lo que un clon superficial
# transfiere. Los dos atajos que parecian equivalentes no lo son:
#
#   el arbol de trabajo   a estas alturas lleva encima los 2.2 GB de cache de modelos que
#                         este ensayo copio al destino, ya contados en sus propias lineas.
#                         Medido asi dio 6 269 MB y el ensayo se reporto como fallado.
#   el .git del clon      clonar de una ruta local copia el almacen de objetos entero e
#                         ignora `--depth 1`, asi que da 256 MB: la historia completa, no lo
#                         que el jurado descarga.
$megas = 0
Push-Location $Destino
$rastreados = @(git ls-files)
Pop-Location
foreach ($rel in $rastreados) {
    $f = Join-Path $Destino $rel
    if (Test-Path $f) { $megas += (Get-Item $f).Length }
}
$megas = [math]::Round($megas / 1MB, 0)
# 10 MB/s es una conexion domestica corriente; a 5 MB/s se dobla, y se dice para no reportar
# el mejor caso como si fuera el unico.
$segundosClon = [math]::Round($megas / 10, 0)
$descargasPendientes += $segundosClon
$notas += ("clonar el repositorio ({0} MB a 10 MB/s): ~{1} s   [a 5 MB/s serian ~{2} s]" -f `
    $megas, $segundosClon, ($segundosClon * 2))

if (-not $SinCache) {
    if ($script:modeloYaEstaba) {
        $descargasPendientes += 150   # 2.4 GB de Phi-3.5 a ~16 MB/s
        $notas += "modelo Phi-3.5 (2.4 GB): ~2.5 min"
    }
    $descargasPendientes += 40        # 427 MB de Piper y voces
    $notas += "Piper + voces (427 MB): ~40 s"
    $descargasPendientes += 90        # 1.3 GB de embeddings + 500 MB de whisper
    $notas += "embeddings e5-large + whisper (1.8 GB): ~1.5 min"
}
$peor = $total + ($descargasPendientes / 60)

if ($notas.Count -gt 0) {
    Write-Host ""
    Write-Host "  Este ensayo reutilizo descargas previas. Estimacion para maquina virgen:"
    foreach ($n in $notas) { Write-Host "    + $n" }
    Write-Host ("  Peor caso estimado: {0:N1} minutos" -f $peor)
    Write-Host "  (corre con -SinCache para medirlo de verdad)"
}

$fallidos = @($pasos | Where-Object { -not $_.OK })
if ($fallidos.Count -gt 0) {
    Write-Host ""
    Write-Host "  PASOS FALLIDOS: $($fallidos.Paso -join ', ')"
}

Write-Host ""
if ($peor -le $LIMITE_MINUTOS -and $fallidos.Count -eq 0) {
    Write-Host ("  COMPUERTA G2: PASA. Margen en el peor caso: {0:N1} minutos" -f ($LIMITE_MINUTOS - $peor))
    exit 0
} else {
    Write-Host "  COMPUERTA G2: NO PASA"
    exit 1
}
