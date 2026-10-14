"""Allowlist-only public deployment archive. Does not publish anything."""
from pathlib import Path
import zipfile


def main():
    root = Path(__file__).resolve().parent.parent
    files = [root / name for name in ("README.md", "requirements.txt", "requirements-dev.txt",
                                      "requirements-gui.txt", "requirements-gui-dev.txt", "GUI使用说明.md",
                                      "gui_app.py", "start_gui.ps1", "启动界面.cmd",
                                      "pyproject.toml", "config.example.yaml", ".gitignore")]
    files.append(root / ".streamlit/config.toml")
    for directory in ("arxiv_tracker", "arxiv_gui", "tools", ".github/workflows", "tests", "examples", "legacy"):
        files.extend(p for p in (root / directory).rglob("*")
                     if p.is_file() and "__pycache__" not in p.parts and "demo-run" not in p.parts
                     and p.suffix in (".py", ".yaml", ".yml", ".md", ".json", ".xml", ".png"))
    with zipfile.ZipFile(root / "deploy-bundle.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(files)):
            archive.write(path, path.relative_to(root).as_posix())
    print(f"Created deploy-bundle.zip: {len(set(files))} allowlisted files.")


if __name__ == "__main__":
    main()
