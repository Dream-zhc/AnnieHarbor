[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$venvPy = Join-Path $Root '.venv\Scripts\python.exe'
$requirements = Join-Path $Root 'requirements-v3.txt'

function Test-Python311([string]$Executable, [string[]]$ExtraArgs) {
    if (-not (Get-Command $Executable -ErrorAction SilentlyContinue)) { return $false }
    try {
        & $Executable @ExtraArgs -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 11))' 2>$null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

if (-not (Test-Path $venvPy)) {
    if (Test-Python311 'py' @('-3.11')) {
        & py -3.11 -m venv (Join-Path $Root '.venv')
    } elseif (Test-Python311 'python' @()) {
        & python -m venv (Join-Path $Root '.venv')
    } else {
        throw 'Python 3.11 is required. Install it on Windows, then run install_win.ps1.'
    }
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create isolated venv.' }
}
& $venvPy -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 11))'
if ($LASTEXITCODE -ne 0) { throw 'The local .venv is not Python 3.11. Rename it and rerun installer.' }

& $venvPy -m pip install -r $requirements
if ($LASTEXITCODE -ne 0) { throw 'Installing standalone viseme dependencies failed.' }
& $venvPy -c 'import dotenv, websocket, pypinyin, g2pw, numpy, huggingface_hub, qwen_asr'
if ($LASTEXITCODE -ne 0) { throw 'Standalone viseme dependency import failed.' }
Write-Host '[VISEME] Dependencies ready.'
Write-Host '[VISEME] Environment is local to this portable folder.'
