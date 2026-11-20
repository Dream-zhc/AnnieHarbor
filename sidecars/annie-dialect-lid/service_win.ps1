[CmdletBinding()]
param()

# 一键：缺环境就装、缺模型就下载，然后启动语种识别服务。推理代码已随文件夹带上（vendor/）。
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Root = $PSScriptRoot
$venvPy = Join-Path $Root '.venv\Scripts\python.exe'
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONUTF8 = '1'

# 见 install_win.ps1：PowerShell 5.1 下“试一下”的调用只看退出码，不让 stderr 中断脚本。
function Invoke-Quiet([string]$Exe, [string[]]$Arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments 2>&1 | Out-Null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $previous
    }
}

$imports = @('-c', 'import torch, numpy, kaldiio, kaldi_native_fbank, websockets, huggingface_hub')
if (-not ((Test-Path $venvPy) -and (Invoke-Quiet $venvPy $imports))) {
    Write-Host '[LID] 第一次运行：准备 Python 环境...'
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'install_win.ps1')
    if ($LASTEXITCODE -ne 0) { throw '环境准备失败，请看上面的提示。' }
}

$model = Join-Path $Root 'models\FireRedLID\model.pth.tar'
if (-not (Test-Path $model)) {
    Write-Host '[LID] 下载 FireRedLID 模型（约 3.5 GB，只需一次；先走 hf-mirror.com 镜像）...'
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $venvPy (Join-Path $Root 'download_model.py')
    $code = $LASTEXITCODE
    $ErrorActionPreference = $previous
    if ($code -ne 0) { throw '模型下载失败，请检查网络后重试（已下载的部分会续传）。' }
}

Write-Host '[LID] 启动语种识别服务，按 Ctrl+C 停止。'
$ErrorActionPreference = 'Continue'
& $venvPy (Join-Path $Root 'lid_service.py')
exit $LASTEXITCODE
