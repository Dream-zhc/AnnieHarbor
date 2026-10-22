[CmdletBinding()]
param(
    [Parameter(Position=0)][string]$RoomHost = '',
    [Parameter(Position=1)][string]$RoomHostB = ''
)

$ErrorActionPreference = 'Stop'
$visemePy = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$modelDir = Join-Path $PSScriptRoot 'models\forced_aligner'

if ($RoomHost) {
    $env:TTS_VISEME_SAM_WS_URL = "ws://$RoomHost/robot/room/reb_sam"
    $env:TTS_VISEME_TTS_WS_URL = "ws://$RoomHost/robot/room/reb_tts"
}
if ($RoomHostB) {
    $roomSuffix = if ($RoomHostB -eq $RoomHost) { '_b' } else { '' }
    $env:TTS_VISEME_SAM_WS_URL_B = "ws://$RoomHostB/robot/room/reb_sam$roomSuffix"
    $env:TTS_VISEME_TTS_WS_URL_B = "ws://$RoomHostB/robot/room/reb_tts$roomSuffix"
}

# An independent service never posts to the main project's local HTTP API.
# Health is published into reb_sam and survives main-project restarts.
$env:TTS_VISEME_OBSERVE_URL = ''
$env:TTS_VISEME_ALIGNER_MODEL = $modelDir
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONUTF8 = '1'
$singletonPort = if ($env:TTS_VISEME_SINGLETON_PORT) { [int]$env:TTS_VISEME_SINGLETON_PORT } else { 37659 }

if (Test-Path $visemePy) {
    # An earlier install may have left a venv behind after failing its final
    # PowerShell/Python -c check. Verify it rather than treating it as ready.
    & $visemePy -c 'import dotenv, websocket, pypinyin, g2pw, numpy, huggingface_hub, qwen_asr'
    $needsInstall = $LASTEXITCODE -ne 0
} else {
    $needsInstall = $true
}
if ($needsInstall) {
    Write-Host '[VISEME] Setting up portable Python environment...'
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'install_win.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'Standalone environment setup failed.' }
}

Set-Location $PSScriptRoot
# Never wait through repeated Hugging Face timeouts during live service boot.
# Import an existing complete Windows cache only; missing models fail promptly.
& $visemePy model_assets.py
if ($LASTEXITCODE -ne 0 -and -not (Test-Path $modelDir)) {
    Write-Host '[VISEME] Looking for a complete locally cached Windows model...'
    & $visemePy model_assets.py --import-cache
}
if ($LASTEXITCODE -ne 0) {
    throw "ForcedAligner is unavailable offline. Place its files under $modelDir or run download_model_win.bat once."
}
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
while ($true) {
    if (Get-NetTCPConnection -State Listen -LocalPort $singletonPort -ErrorAction SilentlyContinue) {
        Write-Host "[VISEME] Singleton port $singletonPort is occupied; another publisher may be running. Refusing duplicate."
        exit 1
    }
    Write-Host '[VISEME] Starting standalone room listener (Ctrl+C stops it).'
    & $visemePy -u (Join-Path $PSScriptRoot 'qwen_stream_room_g2pw.py')
    $code = $LASTEXITCODE
    if ($code -eq 0 -or $code -eq 130) { exit $code }
    Write-Host "[VISEME] Exited with code $code; retrying in 5 seconds." -ForegroundColor Yellow
    Start-Sleep -Seconds 5
}
