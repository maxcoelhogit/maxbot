#!/usr/bin/env python3
from __future__ import annotations

import os
import secrets
import sqlite3
import subprocess
import sys
from pathlib import Path

ENV = Path("/etc/moncoes-alertas/portal.env")
DB = Path("/opt/moncoes-alertas/data/moncoes_alertas.db")


def read_env() -> dict[str, str]:
    out = {}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k] = v
    return out


def need_root():
    if os.geteuid() != 0:
        raise SystemExit("Use sudo para este comando.")


def run(cmd: list[str]):
    return subprocess.run(cmd, check=False)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    env = read_env()

    if cmd == "status":
        run(["systemctl", "--no-pager", "--full", "status", "moncoes-alertas.service"])
        print()
        run(["systemctl", "--no-pager", "--full", "status", "caddy.service"])
    elif cmd == "url":
        print(env.get("PUBLIC_URL", ""))
    elif cmd == "invite":
        need_root()
        print(env.get("DEVICE_INVITE_CODE", ""))
    elif cmd == "devices":
        need_root()
        con = sqlite3.connect(DB)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id,name,created_at,last_seen_at,revoked FROM devices ORDER BY id"
        ).fetchall()
        print("id | nome | criado | ultimo acesso | revogado")
        for r in rows:
            print(f"{r['id']} | {r['name']} | {r['created_at']} | {r['last_seen_at']} | {r['revoked']}")
    elif cmd == "revoke":
        need_root()
        if len(sys.argv) < 3:
            raise SystemExit("Uso: sudo moncoesportal revoke ID")
        con = sqlite3.connect(DB)
        con.execute("UPDATE devices SET revoked=1 WHERE id=?", (int(sys.argv[2]),))
        con.commit()
        print(f"Aparelho {sys.argv[2]} revogado.")
    elif cmd == "rotate-invite":
        need_root()
        new = secrets.token_hex(4).upper()
        lines = ENV.read_text().splitlines()
        lines = [
            f"DEVICE_INVITE_CODE={new}" if x.startswith("DEVICE_INVITE_CODE=") else x
            for x in lines
        ]
        ENV.write_text("\n".join(lines) + "\n")
        run(["systemctl", "restart", "moncoes-alertas.service"])
        print(f"Novo codigo: {new}")
    elif cmd == "sync":
        need_root()
        run(["systemctl", "start", "moncoes-ai-health.service"])
        run(["systemctl", "start", "moncoes-ai-portal-sync.service"])
        run(["journalctl", "-u", "moncoes-ai-portal-sync.service", "-n", "20", "--no-pager"])
    elif cmd == "test-push":
        need_root()
        run([
            "systemd-run", "--wait", "--pipe", "--collect",
            "--unit=moncoes-alertas-push-test",
            "--property=User=moncoesalertas",
            "--property=Group=moncoesalertas",
            "--property=EnvironmentFile=/etc/moncoes-alertas/portal.env",
            "--property=WorkingDirectory=/opt/moncoes-alertas",
            "/opt/moncoes-alertas/venv/bin/python", "-c",
            'from app import send_push; print("push enviados:", send_push("Teste - Moncoes Alertas","Notificacoes do Moncoes Alertas estao funcionando.","/"))'
        ])
    elif cmd == "logs":
        n = sys.argv[2] if len(sys.argv) > 2 else "100"
        run(["journalctl", "-u", "moncoes-alertas.service", "-n", n, "--no-pager"])
    else:
        raise SystemExit(
            "Uso: moncoesportal {status|url|invite|devices|revoke ID|rotate-invite|sync|test-push|logs [N]}"
        )


if __name__ == "__main__":
    main()
