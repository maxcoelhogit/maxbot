from __future__ import annotations

import json
import os
import socket

import requests
import subprocess
import time

from common import DATA_DIR, now_local

TARGETS = [
    ("192.168.10.1", 443),
    ("192.168.10.100", 554),
    ("192.168.10.101", 554),
    ("192.168.10.100", 37777),
    ("192.168.10.101", 37777),
]


def tcp(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except Exception:
        return False


def handshake_age():
    try:
        output = subprocess.check_output(
            ["wg", "show", "wg0", "latest-handshakes"],
            text=True,
        ).strip().splitlines()
        timestamps = [
            int(line.split("\t")[1])
            for line in output
            if "\t" in line
        ]
        if not timestamps:
            return None
        return int(time.time()) - max(timestamps)
    except Exception:
        return None


age = handshake_age()
results = {f"{host}:{port}": tcp(host, port) for host, port in TARGETS}
healthy = age is not None and age < 180 and all(results.values())

payload = {
    "time": now_local().isoformat(),
    "healthy": healthy,
    "wireguard_handshake_age_seconds": age,
    "targets": results,
}

DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "health.json").write_text(
    json.dumps(payload, indent=2),
    encoding="utf-8",
)

portal = os.getenv("PORTAL_BASE_URL", "").strip().rstrip("/")
token = os.getenv("PORTAL_INGEST_TOKEN", "").strip()
if portal and token:
    try:
        requests.post(
            f"{portal}/api/ingest/health",
            json={"payload": payload},
            headers={"X-Ingest-Token": token},
            timeout=8,
        ).raise_for_status()
    except Exception:
        pass

print(json.dumps(payload, indent=2))
raise SystemExit(0 if healthy else 1)
