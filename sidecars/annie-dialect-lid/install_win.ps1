[CmdletBinding()]
param()

# 只在本文件夹里建 Python 3.11 虚拟环境：CUDA 版 PyTorch + 少量依赖；不动系统 Python。
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$venvPy = Join-Path $Root '.venv\Scripts\python.exe'

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
        throw '需要 Python 3.11：请先在这台 Windows 上安装 Python 3.11，再运行 start_win.bat。'
    }
    if ($LASTEXITCODE -ne 0) { throw '创建虚拟环境失败。' }
}

& $venvPy -m pip install --upgrade pip
& $venvPy -c 'import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)' 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host '[LID] 安装 CUDA 版 PyTorch（约 2.5 GB，只需一次）...'
    & $venvPy -m pip install torch==2.5.1 --index-url https://download.pytorch.org/whl/cu121
    if ($LASTEXITCODE -ne 0) {
        Write-Host '[LID] 官方源失败，改用阿里云镜像...'
        & $venvPy -m pip install torch==2.5.1 -f https://mirrors.aliyun.com/pytorch-wheels/cu121/
        if ($LASTEXITCODE -ne 0) { throw '安装 PyTorch 失败。' }
    }
}

& $venvPy -m pip install -r (Join-Path $Root 'requirements.txt')
if ($LASTEXITCODE -ne 0) {
    & $venvPy -m pip install -r (Join-Path $Root 'requirements.txt') -i https://mirrors.aliyun.com/pypi/simple/
    if ($LASTEXITCODE -ne 0) { throw '安装依赖失败。' }
}
& $venvPy -c 'import torch, numpy, kaldiio, kaldi_native_fbank, websockets, huggingface_hub; print("[LID] CUDA:", torch.cuda.is_available())'
if ($LASTEXITCODE -ne 0) { throw '依赖导入检查失败。' }
Write-Host '[LID] 依赖就绪（只装在本文件夹的 .venv 里）。'
