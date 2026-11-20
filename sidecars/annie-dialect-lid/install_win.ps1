[CmdletBinding()]
param()

# 只在本文件夹里建 Python 3.11 虚拟环境：CUDA 版 PyTorch + 少量依赖；不动系统 Python。
# 国内网络先走阿里云镜像，失败再走官方源。
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Root = $PSScriptRoot
$venvPy = Join-Path $Root '.venv\Scripts\python.exe'
$pypiMirror = 'https://mirrors.aliyun.com/pypi/simple/'
$torchMirror = 'https://mirrors.aliyun.com/pytorch-wheels/cu121/'

# Windows PowerShell 5.1 把原生程序写到 stderr 的内容当成错误；“出错就停”时会直接中断脚本。
# 所有“试一下能不能成功”的调用都走这里，只看退出码。
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

function Invoke-Loud([string]$Exe, [string[]]$Arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        # Out-Host: shown on screen, but never becomes this function's return value
        & $Exe @Arguments | Out-Host
        return $LASTEXITCODE -eq 0
    } catch {
        Write-Host $_
        return $false
    } finally {
        $ErrorActionPreference = $previous
    }
}

$check311 = @('-c', 'import sys; raise SystemExit(sys.version_info[:2] != (3, 11))')
if (-not (Test-Path $venvPy)) {
    if ((Get-Command 'py' -ErrorAction SilentlyContinue) -and (Invoke-Quiet 'py' (@('-3.11') + $check311))) {
        $ok = Invoke-Loud 'py' @('-3.11', '-m', 'venv', (Join-Path $Root '.venv'))
    } elseif ((Get-Command 'python' -ErrorAction SilentlyContinue) -and (Invoke-Quiet 'python' $check311)) {
        $ok = Invoke-Loud 'python' @('-m', 'venv', (Join-Path $Root '.venv'))
    } else {
        throw '需要 Python 3.11：请先在这台 Windows 上安装 Python 3.11，再运行 start_win.bat。'
    }
    if (-not $ok) { throw '创建虚拟环境失败。' }
}
if (-not (Invoke-Quiet $venvPy $check311)) { throw '本文件夹的 .venv 不是 Python 3.11：删掉 .venv 文件夹后重试。' }

$null = Invoke-Loud $venvPy @('-m', 'pip', 'install', '--upgrade', 'pip', '-i', $pypiMirror)

if (-not (Invoke-Quiet $venvPy @('-c', 'import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)'))) {
    Write-Host '[LID] 安装 CUDA 版 PyTorch（约 2.5 GB，只需一次）...'
    $ok = Invoke-Loud $venvPy @('-m', 'pip', 'install', 'torch==2.5.1+cu121', '-f', $torchMirror, '-i', $pypiMirror)
    if (-not $ok) {
        Write-Host '[LID] 阿里云镜像失败，改用 PyTorch 官方源...'
        $ok = Invoke-Loud $venvPy @('-m', 'pip', 'install', 'torch==2.5.1', '--index-url', 'https://download.pytorch.org/whl/cu121')
    }
    if (-not $ok) { throw '安装 PyTorch 失败，请检查网络后重试。' }
}

$requirements = Join-Path $Root 'requirements.txt'
if (-not (Invoke-Loud $venvPy @('-m', 'pip', 'install', '-r', $requirements, '-i', $pypiMirror))) {
    if (-not (Invoke-Loud $venvPy @('-m', 'pip', 'install', '-r', $requirements))) { throw '安装依赖失败。' }
}
if (-not (Invoke-Loud $venvPy @('-c', 'import torch, numpy, kaldiio, kaldi_native_fbank, websockets, huggingface_hub; print(''[LID] CUDA available:'', torch.cuda.is_available())'))) {
    throw '依赖导入检查失败。'
}
Write-Host '[LID] 依赖就绪（只装在本文件夹的 .venv 里）。'
