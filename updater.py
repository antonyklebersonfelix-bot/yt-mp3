"""Atualização do próprio app via GitHub Releases.

Cada release tem dois arquivos: latest.json ({"version", "notes"}) e YT-MP3.zip.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from version import REPO, VERSION

ZIP_NAME = "YT-MP3.zip"
CHECK_EVERY = 6 * 3600

state = {"current": VERSION, "available": None, "notes": "", "status": "", "message": ""}
_lock = threading.Lock()


def _set(**kw):
    with _lock:
        state.update(kw)


def snapshot():
    with _lock:
        return dict(state)


def _vt(v):
    return tuple(int(x) for x in v.lstrip("v").split(".") if x.isdigit())


def check():
    if not REPO:
        return
    try:
        out = subprocess.run(
            ["/usr/bin/curl", "-fsSL", "--max-time", "20",
             f"https://github.com/{REPO}/releases/latest/download/latest.json"],
            capture_output=True, text=True)
        if out.returncode != 0:
            return
        data = json.loads(out.stdout)
        if _vt(data["version"]) > _vt(VERSION):
            _set(available=data["version"], notes=data.get("notes", ""))
    except Exception:  # noqa: BLE001 — sem internet etc.: tenta depois
        pass


def check_loop():
    while True:
        check()
        time.sleep(CHECK_EVERY)


def app_bundle():
    if not getattr(sys, "frozen", False):
        return None
    p = Path(sys.executable).resolve()
    for parent in p.parents:
        if parent.suffix == ".app":
            return parent
    return None


def apply():
    """Baixa a versão nova, troca o app e reabre. Roda numa thread."""
    ver = state["available"]
    bundle = app_bundle()
    if not ver:
        return
    if not bundle:
        _set(status="erro", message="Atualização automática só funciona no app instalado.")
        return
    if not os.access(bundle.parent, os.W_OK):
        _set(status="erro", message=f"Sem permissão para atualizar em {bundle.parent}. "
                                    "Baixe a versão nova e arraste para Aplicativos.")
        return
    try:
        _set(status="baixando", message=f"Baixando a versão {ver}…")
        tmp = Path(tempfile.mkdtemp(prefix="ytmp3-update-"))
        zpath = tmp / ZIP_NAME
        subprocess.run(["/usr/bin/curl", "-fsSL", "--retry", "2", "-o", str(zpath),
                        f"https://github.com/{REPO}/releases/download/v{ver}/{ZIP_NAME}"], check=True)
        _set(message="Instalando…")
        subprocess.run(["/usr/bin/ditto", "-x", "-k", str(zpath), str(tmp / "x")], check=True)
        new = next((tmp / "x").glob("*.app"))
        script = tmp / "swap.sh"
        script.write_text(f'''#!/bin/sh
while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.3; done
OLD="{bundle}"
mv "$OLD" "{tmp}/old.app" && mv "{new}" "$OLD" || mv "{tmp}/old.app" "$OLD"
xattr -dr com.apple.quarantine "$OLD" 2>/dev/null
open "$OLD"
rm -rf "{tmp}"
''')
        subprocess.Popen(["/bin/sh", str(script)], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _set(status="reiniciando", message="Reabrindo o app…")
        time.sleep(1.5)  # deixa a tela mostrar a mensagem
        os._exit(0)
    except Exception as e:  # noqa: BLE001
        _set(status="erro", message=f"Não consegui atualizar ({e}). Tente de novo mais tarde.")


def start():
    threading.Thread(target=check_loop, daemon=True).start()


def start_apply():
    if state["status"] in ("baixando", "reiniciando"):
        return
    threading.Thread(target=apply, daemon=True).start()
