from __future__ import annotations

import json
import os
from pathlib import Path

import requests

from common import DATA_DIR, REPORT_DIR, db

PORTAL = os.getenv("PORTAL_BASE_URL", "").strip().rstrip("/")
TOKEN = os.getenv("PORTAL_INGEST_TOKEN", "").strip()


def post_json(path: str, payload: dict):
    r = requests.post(
        f"{PORTAL}{path}",
        json=payload,
        headers={"X-Ingest-Token": TOKEN},
        timeout=12,
    )
    r.raise_for_status()
    return r.json() if r.content else {}


def main() -> int:
    if not PORTAL or not TOKEN:
        print("Portal não configurado.")
        return 2

    sent = 0
    skipped = 0
    with db() as con:
        rows = con.execute(
            """SELECT * FROM events
               WHERE error IS NULL AND final_status IS NOT NULL
               ORDER BY started_at ASC"""
        ).fetchall()

    for row in rows:
        payload = {
            "event_key": row["event_key"],
            "occurred_at": row["started_at"],
            "dvr": row["dvr"],
            "channel": row["channel"],
            "camera": row["camera_name"],
            "status": row["final_status"],
            "confidence": row["confidence"],
            "category": row["category"],
            "description": row["description"],
            "rule_reference": row["rule_reference"],
            "needs_human_review": bool(row["needs_human_review"]),
            "source": row["source"],
        }
        try:
            post_json("/api/ingest/event", payload)
            sent += 1
        except Exception as exc:
            skipped += 1
            print(f"Falha evento {row['event_key']}: {exc}")

    health = DATA_DIR / "health.json"
    if health.exists():
        try:
            post_json("/api/ingest/health", {"payload": json.loads(health.read_text())})
        except Exception as exc:
            print(f"Falha health: {exc}")

    reports = 0
    for path in sorted(REPORT_DIR.glob("*.pdf"))[-30:]:
        kind = "weekly" if "weekly" in path.name else "daily"
        try:
            with path.open("rb") as fh:
                r = requests.post(
                    f"{PORTAL}/api/ingest/report",
                    params={"kind": kind},
                    files={"file": (path.name, fh, "application/pdf")},
                    headers={"X-Ingest-Token": TOKEN},
                    timeout=30,
                )
                r.raise_for_status()
                reports += 1
        except Exception as exc:
            print(f"Falha relatório {path.name}: {exc}")

    print(f"Sincronização concluída: eventos={sent}, falhas={skipped}, relatórios={reports}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
