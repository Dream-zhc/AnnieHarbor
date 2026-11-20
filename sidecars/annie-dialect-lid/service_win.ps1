[CmdletBinding()]
param()

# 一键：缺环境就装、缺代码和模型就下载，然后启动语种识别服务。
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$venvPy = Join-Path $Root '.venv\Scripts\python.exe'
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONUTF8 = '1'

$ready = $false
if (Test-Path $venvPy) {
    & $venvPy -c 'import torch, numpy, kaldiio, kaldi_native_fbank, websockets, huggingface_hub' 2>$null
    $ready = $LASTEXITCODE -eq 0
}
if (-not $ready) {
    Write-Host '[LID] 第一次运行：准备 Python 环境...'
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'install_win.ps1')
    if ($LASTEXITCODE -ne 0) { throw '环境准备失败。' }
}

$code = Join-Path $Root 'FireRedASR2S\fireredasr2s\fireredlid\lid.py'
if (-not (Test-Path $code)) {
    Write-Host '[LID] 下载 FireRedASR2S 推理代码（Apache-2.0）...'
    $zip = Join-Path $Root 'FireRedASR2S.zip'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/FireRedTeam/FireRedASR2S/archive/refs/heads/main.zip' -OutFile $zip
    $unpack = Join-Path $Root 'FireRedASR2S-unpack'
    if (Test-Path $unpack) { Remove-Item -Recurse -Force $unpack }
    Expand-Archive -Path $zip -DestinationPath $unpack
    Move-Item -Path (Join-Path $unpack 'FireRedASR2S-main') -Destination (Join-Path $Root 'FireRedASR2S')
    Remove-Item -Recurse -Force $unpack
    Remove-Item -Force $zip
    if (-not (Test-Path $code)) { throw '代码下载失败：请手动把 FireRedASR2S 仓库放到本文件夹的 FireRedASR2S 目录。' }
}

$model = Join-Path $Root 'models\FireRedLID\model.pth.tar'
if (-not (Test-Path $model)) {
    Write-Host '[LID] 下载 FireRedLID 模型（约 3.5 GB，只需一次）...'
    & $venvPy (Join-Path $Root 'download_model.py')
    if ($LASTEXITCODE -ne 0) { throw '模型下载失败，请检查网络后重试。' }
}

Write-Host '[LID] 启动语种识别服务，按 Ctrl+C 停止。'
& $venvPy (Join-Path $Root 'lid_service.py')
exit $LASTEXITCODE
