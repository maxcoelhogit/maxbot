#!/usr/bin/env python3
from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import sys
import time
from pathlib import Path

PKG = Path(__file__).resolve().parent
BASE = Path("/opt/moncoes-alertas")
SERVICE_USER = "moncoesalertas"


def run(cmd, check=True):
    print("+", " ".join(str(x) for x in cmd))
    return subprocess.run([str(x) for x in cmd], check=check)


def main():
    if os.geteuid() != 0:
        raise SystemExit("Execute com: sudo python3 update_vm.py")

    required = [
        PKG / "app.py",
        PKG / "requirements.txt",
        PKG / "portalctl.py",
        PKG / "static",
    ]
    missing = [str(x) for x in required if not x.exists()]
    if missing:
        raise SystemExit("Arquivos ausentes: " + ", ".join(missing))

    print("Atualizando Moncoes Alertas sem alterar dados, chaves ou configuracao...")

    # Validate syntax before touching the running installation.
    run([sys.executable, "-m", "py_compile", PKG / "app.py", PKG / "portalctl.py"])

    if not (BASE / "venv/bin/pip").exists():
        raise SystemExit("Instalacao existente nao encontrada em /opt/moncoes-alertas")

    run([BASE / "venv/bin/pip", "install", "-r", PKG / "requirements.txt"])

    run(["systemctl", "stop", "moncoes-alertas.service"])

    shutil.copy2(PKG / "app.py", BASE / "app.py")
    shutil.copy2(PKG / "requirements.txt", BASE / "requirements.txt")

    static_target = BASE / "static"
    backup_target = BASE / "static.previous"
    if backup_target.exists():
        shutil.rmtree(backup_target)
    if static_target.exists():
        shutil.move(static_target, backup_target)
    shutil.copytree(PKG / "static", static_target)

    shutil.copy2(PKG / "portalctl.py", "/usr/local/bin/moncoesportal")
    os.chmod("/usr/local/bin/moncoesportal", 0o755)

    user = pwd.getpwnam(SERVICE_USER)
    for root, dirs, files in os.walk(BASE):
        # Do not change ownership of the virtualenv unnecessarily.
        if str(root).startswith(str(BASE / "venv")):
            continue
        try:
            os.chown(root, user.pw_uid, user.pw_gid)
        except PermissionError:
            pass
        for name in dirs:
            p = Path(root) / name
            if not str(p).startswith(str(BASE / "venv")):
                try:
                    os.chown(p, user.pw_uid, user.pw_gid)
                except PermissionError:
                    pass
        for name in files:
            p = Path(root) / name
            if not str(p).startswith(str(BASE / "venv")):
                try:
                    os.chown(p, user.pw_uid, user.pw_gid)
                except PermissionError:
                    pass

    run(["systemctl", "start", "moncoes-alertas.service"])
    run(["systemctl", "restart", "caddy"], check=False)

    # Verify that the new endpoints really exist. Uvicorn may need a few
    # seconds after systemd reports the service as started, especially on
    # the small e2-micro VM.
    import urllib.request
    import json

    spec = None
    last_exc = None
    for attempt in range(1, 21):
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:8088/openapi.json",
                timeout=3,
            ) as r:
                spec = json.loads(r.read().decode())
            break
        except Exception as exc:
            last_exc = exc
            if attempt < 20:
                time.sleep(1)

    if spec is None:
        print("ERRO: o servico nao ficou pronto apos 20 segundos:", last_exc)
        run(
            [
                "systemctl", "--no-pager", "--full",
                "status", "moncoes-alertas.service",
            ],
            check=False,
        )
        run(
            [
                "journalctl", "-u", "moncoes-alertas.service",
                "-n", "80", "--no-pager",
            ],
            check=False,
        )
        raise SystemExit(1)

    paths = spec.get("paths", {})
    expected = [
        "/api/push/test",
        "/api/reports/{report_id}/link",
        "/seguranca/",
        "/public/api/status",
        "/public/api/events",
        "/public/api/push/subscribe",
        "/public/api/push/test",
    ]
    absent = [p for p in expected if p not in paths]
    if absent:
        print("ERRO: rotas ausentes apos update:", ", ".join(absent))
        raise SystemExit(1)

    print("Rotas verificadas: administrativo + Moncoes Seguranca publico")

    print("Atualizacao concluida com sucesso.")


if __name__ == "__main__":
    main()
