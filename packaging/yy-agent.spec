# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH).resolve().parent
if not (root / "run.py").is_file():
    root = root.parent
extension_datas = [
    (str(path), str(path.parent.relative_to(root)))
    for path in (root / "extension").rglob("*")
    if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
]

a = Analysis(
    [str(root / "run.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[
        (str(root / "bootstrap" / "templates"), "bootstrap/templates"),
        (str(root / "skills"), "skills"),
        (str(root / "sandbox" / "Dockerfile"), "sandbox"),
        (str(root / "ui" / "dist"), "ui/dist"),
        (str(root / "run_ui" / "templates"), "run_ui/templates"),
        (str(root / "harness-evolution" / "harness.py"), "harness-evolution"),
    ] + extension_datas,
    hiddenimports=[
        "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
    ] + collect_submodules("backup") + collect_submodules("cryptography"),
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="yy-agent",
    console=True,
)
