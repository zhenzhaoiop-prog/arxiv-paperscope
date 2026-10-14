"""Windows PowerShell 5.1 requires a BOM for scripts containing Chinese."""
import codecs
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_launcher_encoding():
    raw = (ROOT / "start_gui.ps1").read_bytes()
    assert raw.startswith(codecs.BOM_UTF8)
    assert "PaperScope 即将启动" in raw.decode("utf-8-sig")


def test_missing_dependencies_do_not_raise_powershell_stderr_error():
    text = (ROOT / "start_gui.ps1").read_text(encoding="utf-8-sig")
    assert "importlib.util.find_spec" in text
    assert "2>$null" not in text
    executable = shutil.which("powershell.exe")
    if executable is None:
        pytest.skip("Windows PowerShell is unavailable on this platform")
    import sys
    probe = (
        "$ErrorActionPreference='Stop'; & '" + sys.executable.replace("'", "''")
        + "' -c \"import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('paperscope_missing_dependency_test') else 1)\"; "
        "if ($LASTEXITCODE -eq 1) { Write-Output 'INSTALL_REQUIRED'; exit 0 }; exit 2"
    )
    result = subprocess.run([executable, "-NoProfile", "-Command", probe], capture_output=True, timeout=30)
    assert result.returncode == 0
    assert b"INSTALL_REQUIRED" in result.stdout


def test_windows_powershell_parses_launcher():
    executable = shutil.which("powershell.exe")
    if executable is None:
        pytest.skip("Windows PowerShell is unavailable on this platform")
    command = (
        "$tokens=$null; $errors=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        "(Join-Path (Get-Location) 'start_gui.ps1'), [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count) { $errors | Out-String | Write-Output; exit 1 }"
    )
    result = subprocess.run(
        [executable, "-NoProfile", "-Command", command], cwd=ROOT,
        capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout.decode(errors="replace")
