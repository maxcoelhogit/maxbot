#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PKG = Path(__file__).resolve().parent
BASE = Path("/opt/moncoes-alertas")
ETC = Path("/etc/moncoes-alertas")
ENV = ETC / "portal.env"
SERVICE_USER = "moncoesalertas"
PORT = 8088


def run(cmd, check=True, capture=False):
    print("+", " ".join(str(x) for x in cmd))
    return subprocess.run(
        [str(x) for x in cmd],
        check=check,
        text=True,
        capture_output=capture,
    )


def shell(command: str, check=True):
    print("+", command)
    return subprocess.run(["bash", "-lc", command], check=check, text=True)


def metadata_external_ip() -> str:
    req = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/"
        "network-interfaces/0/access-configs/0/external-ip",
        headers={"Metadata-Flavor": "Google"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.read().decode().strip()
    except Exception:
        return ""


def parse_env(path: Path) -> dict[str, str]:
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k] = v
    return out


def write_env(values: dict[str, str]):
    lines = [f"{k}={v}" for k, v in values.items()]
    ENV.write_text("\n".join(lines) + "\n")
    os.chmod(ENV, 0o640)
    uid = 0
    gid = pwd.getpwnam(SERVICE_USER).pw_gid
    os.chown(ENV, uid, gid)


def ensure_caddy():
    if shutil.which("caddy"):
        return
    r = subprocess.run(
        ["apt-get", "install", "-y", "caddy"],
        text=True,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    if r.returncode == 0:
        return
    shell("apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg")
    shell(
        "curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key "
        "| gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg"
    )
    shell(
        "curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt "
        "> /etc/apt/sources.list.d/caddy-stable.list"
    )
    shell("chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg")
    run(["apt-get", "update", "-y"])
    run(["apt-get", "install", "-y", "caddy"])


def generate_secrets() -> dict[str, str]:
    code = r"""
import base64, json, secrets
from cryptography.hazmat.primitives.asymmetric import ec

def b64u(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")

key = ec.generate_private_key(ec.SECP256R1())
nums = key.private_numbers()
priv = nums.private_value.to_bytes(32, "big")
pubnums = nums.public_numbers
pub = b"\x04" + pubnums.x.to_bytes(32, "big") + pubnums.y.to_bytes(32, "big")
print(json.dumps({
    "VAPID_PRIVATE_KEY": b64u(priv),
    "VAPID_PUBLIC_KEY": b64u(pub),
    "INGEST_TOKEN": secrets.token_urlsafe(32),
    "DEVICE_INVITE_CODE": secrets.token_hex(8).upper(),
}))
"""
    r = run([BASE / "venv/bin/python", "-c", code], capture=True)
    return json.loads(r.stdout)


def ensure_service_user():
    try:
        pwd.getpwnam(SERVICE_USER)
    except KeyError:
        run([
            "useradd", "--system", "--home", str(BASE),
            "--shell", "/usr/sbin/nologin", SERVICE_USER,
        ])


def write_systemd():
    unit = f"""[Unit]
Description=Moncoes Alertas PWA
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={SERVICE_USER}
Group={SERVICE_USER}
EnvironmentFile={ENV}
WorkingDirectory={BASE}
ExecStart={BASE}/venv/bin/uvicorn app:app --host 127.0.0.1 --port {PORT}
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths={BASE}/data

[Install]
WantedBy=multi-user.target
"""
    Path("/etc/systemd/system/moncoes-alertas.service").write_text(unit)


def write_caddy(host: str):
    path = Path("/etc/caddy/Caddyfile")
    if path.exists():
        backup = path.with_name(f"Caddyfile.backup.{int(time.time())}")
        shutil.copy2(path, backup)
    path.write_text(
        f"""{host} {{
    encode zstd gzip
    reverse_proxy 127.0.0.1:{PORT}
    header {{
        X-Content-Type-Options nosniff
        Referrer-Policy no-referrer
        Permissions-Policy "camera=(), microphone=(), geolocation=()"
    }}
}}
"""
    )


def integrate_monitor(ingest_token: str):
    mon_env = Path("/etc/moncoes-ai/moncoes.env")
    if not mon_env.exists():
        print("Aviso: /etc/moncoes-ai/moncoes.env nao encontrado; integracao adiada.")
        return

    lines = [
        x for x in mon_env.read_text().splitlines()
        if not x.startswith("PORTAL_BASE_URL=")
        and not x.startswith("PORTAL_INGEST_TOKEN=")
    ]
    lines += [
        f"PORTAL_BASE_URL=http://127.0.0.1:{PORT}",
        f"PORTAL_INGEST_TOKEN={ingest_token}",
    ]
    mon_env.write_text("\n".join(lines) + "\n")

    sync_unit = """[Unit]
Description=Moncoes AI -> Moncoes Alertas initial sync
After=moncoes-alertas.service

[Service]
Type=oneshot
User=moncoesai
Group=moncoesai
EnvironmentFile=/etc/moncoes-ai/moncoes.env
WorkingDirectory=/opt/moncoes-ai/app
ExecStart=/opt/moncoes-ai/venv/bin/python /opt/moncoes-ai/app/portal_sync.py
"""
    Path("/etc/systemd/system/moncoes-ai-portal-sync.service").write_text(sync_unit)


def install_cli():
    target = Path("/usr/local/bin/moncoesportal")
    shutil.copy2(PKG / "portalctl.py", target)
    target.chmod(0o755)


def main():
    if os.geteuid() != 0:
        raise SystemExit("Execute com: sudo python3 install_vm.py [host-opcional]")

    custom_host = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    if not custom_host:
        ip = metadata_external_ip()
        if not ip:
            raise SystemExit(
                "Nao foi possivel detectar o IPv4 externo. "
                "Passe um host: sudo python3 install_vm.py alertas.seudominio.com.br"
            )
        custom_host = f"{ip}.sslip.io"
    public_url = f"https://{custom_host}"

    print("============================================")
    print(" Moncoes Alertas PWA - instalacao na VM")
    print("============================================")

    run(["apt-get", "update", "-y"])
    run([
        "apt-get", "install", "-y",
        "python3-venv", "python3-pip", "sqlite3",
        "curl", "ca-certificates", "gnupg",
    ])
    ensure_caddy()
    ensure_service_user()

    (BASE / "static").mkdir(parents=True, exist_ok=True)
    (BASE / "data/reports").mkdir(parents=True, exist_ok=True)
    ETC.mkdir(parents=True, exist_ok=True)

    shutil.copy2(PKG / "app.py", BASE / "app.py")
    shutil.copy2(PKG / "requirements.txt", BASE / "requirements.txt")
    if (BASE / "static").exists():
        shutil.rmtree(BASE / "static")
    shutil.copytree(PKG / "static", BASE / "static")

    run(["python3", "-m", "venv", BASE / "venv"])
    run([BASE / "venv/bin/pip", "install", "--upgrade", "pip", "wheel"])
    run([BASE / "venv/bin/pip", "install", "-r", BASE / "requirements.txt"])

    values = parse_env(ENV)
    if not values:
        values = {
            "DATA_DIR": str(BASE / "data"),
            **generate_secrets(),
            "VAPID_SUBJECT": "mailto:admin@edificiomoncoes.com.br",
        }
    values["PUBLIC_URL"] = public_url
    write_env(values)

    uid = pwd.getpwnam(SERVICE_USER).pw_uid
    gid = pwd.getpwnam(SERVICE_USER).pw_gid
    for root, dirs, files in os.walk(BASE):
        os.chown(root, uid, gid)
        for name in dirs:
            os.chown(Path(root) / name, uid, gid)
        for name in files:
            os.chown(Path(root) / name, uid, gid)

    write_systemd()
    write_caddy(custom_host)
    integrate_monitor(values["INGEST_TOKEN"])
    install_cli()

    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", "moncoes-alertas.service"])
    run(["systemctl", "enable", "--now", "caddy"])
    run(["systemctl", "restart", "caddy"])
    run(["systemctl", "restart", "moncoes-ai-monitor.service"], check=False)
    run(["systemctl", "start", "moncoes-ai-health.service"], check=False)
    time.sleep(2)
    run(["systemctl", "start", "moncoes-ai-portal-sync.service"], check=False)

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=5) as r:
            local_health = r.read().decode()
    except Exception as exc:
        local_health = f"ERRO: {exc}"

    print()
    print("== Servico local ==")
    print(local_health)
    print()
    print("============================================")
    print(" Moncoes Alertas instalado")
    print("============================================")
    print(f"URL publica:    {public_url}")
    print(f"Codigo convite: {values['DEVICE_INVITE_CODE']}")
    print()
    print("IMPORTANTE: no Google Cloud, TCP 80 e 443 precisam estar liberados para esta VM.")
    print("Antes de instalar nos celulares, mantenha o IPv4 externo estavel/reservado.")
    print()
    print("Status:        sudo moncoesportal status")
    print("URL:           sudo moncoesportal url")
    print("Convite:       sudo moncoesportal invite")
    print("Dispositivos:  sudo moncoesportal devices")
    print("Teste push:    sudo moncoesportal test-push")


if __name__ == "__main__":
    main()
