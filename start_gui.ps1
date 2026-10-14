$ErrorActionPreference = 'Stop'
$guiProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $guiProjectRoot
$guiPython = Join-Path $guiProjectRoot '.venv-gui\Scripts\python.exe'
try {
    if (-not (Test-Path -LiteralPath $guiPython)) {
        Write-Host '首次启动：正在创建独立 GUI Python 环境...'
        $guiSystemPython = Get-Command python -ErrorAction Stop
        & $guiSystemPython.Source -c "import sys; assert sys.version_info >= (3,11), 'Requires Python 3.11+'"
        if ($LASTEXITCODE -ne 0) { throw '需要 Python 3.11 或更新版本。' }
        & $guiSystemPython.Source -m venv .venv-gui
        if ($LASTEXITCODE -ne 0) { throw '创建虚拟环境失败。' }
    }
    & $guiPython -c "import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(name) is not None for name in ['streamlit', 'pandas', 'yaml', 'requests', 'pypdf', 'tzdata']) else 1)"
    if ($LASTEXITCODE -ne 0) {
        Write-Host '正在安装界面依赖（首次安装需要网络）...'
        & $guiPython -m pip install -r requirements-gui.txt
        if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请检查网络或 pip 镜像配置后重试。' }
    }
    Write-Host 'PaperScope 即将启动：http://127.0.0.1:8501'
    Write-Host '关闭本窗口将停止本地界面；GitHub 云端自动任务不受影响。'
    & $guiPython -m streamlit run gui_app.py --server.address 127.0.0.1 --server.headless true
    if ($LASTEXITCODE -ne 0) { throw '界面进程启动失败，请查看上方错误信息。' }
} catch {
    Write-Host ('启动失败：' + $_.Exception.Message) -ForegroundColor Red
    Read-Host '按回车退出，然后可重新双击启动脚本'
    exit 1
}
