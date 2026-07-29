# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

root = Path(SPECPATH).resolve().parent

a = Analysis(
    [str(root / "run.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[
        (str(root / "bootstrap" / "templates"), "bootstrap/templates"),
        (str(root / "sandbox" / "Dockerfile"), "sandbox"),
        (str(root / "ui" / "dist"), "ui/dist"),
        (str(root / "run_ui" / "templates"), "run_ui/templates"),
    ],
    hiddenimports=["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets.auto"],
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
