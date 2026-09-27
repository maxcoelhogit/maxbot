from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pywebpush import WebPushException, webpush

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
DATA = Path(os.getenv("DATA_DIR", "/data"))
REPORTS = DATA / "reports"
DB = DATA / "moncoes_alertas.db"

DEVICE_INVITE_CODE = os.getenv("DEVICE_INVITE_CODE", "")
INGEST_TOKEN = os.getenv("INGEST_TOKEN", "")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:admin@example.com")

DATA.mkdir(parents=True, exist_ok=True)
REPORTS.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS devices (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          token_hash TEXT UNIQUE NOT NULL,
          created_at TEXT NOT NULL,
          last_seen_at TEXT,
          revoked INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          device_id INTEGER NOT NULL,
          endpoint TEXT UNIQUE NOT NULL,
          p256dh TEXT NOT NULL,
          auth TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          event_key TEXT UNIQUE NOT NULL,
          received_at TEXT NOT NULL,
          occurred_at TEXT,
          dvr TEXT,
          channel INTEGER,
          camera TEXT,
          status TEXT,
          confidence REAL,
          category TEXT,
          description TEXT,
          rule_reference TEXT,
          needs_human_review INTEGER NOT NULL DEFAULT 0,
          acknowledged_at TEXT,
          acknowledged_by INTEGER,
          source TEXT,
          FOREIGN KEY(acknowledged_by) REFERENCES devices(id)
        );

        CREATE TABLE IF NOT EXISTS health (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1),
          received_at TEXT NOT NULL,
          payload TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reports (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT NOT NULL,
          filename TEXT NOT NULL,
          created_at TEXT NOT NULL,
          period_start TEXT,
          period_end TEXT,
          UNIQUE(kind, filename)
        );
        """
    )
    return con


class RegisterBody(BaseModel):
    invite_code: str
    device_name: str = Field(min_length=2, max_length=80)


class SubscriptionBody(BaseModel):
    endpoint: str
    keys: dict[str, str]


class IngestEvent(BaseModel):
    event_key: str
    occurred_at: str | None = None
    dvr: str | None = None
    channel: int | None = None
    camera: str | None = None
    status: str | None = None
    confidence: float | None = None
    category: str | None = None
    description: str | None = None
    rule_reference: str | None = None
    needs_human_review: bool = False
    source: str | None = None
    notify_external: bool = False


class HealthBody(BaseModel):
    payload: dict[str, Any]


def require_ingest(x_ingest_token: str | None = Header(default=None)) -> None:
    if not INGEST_TOKEN or not x_ingest_token or not hmac.compare_digest(
        x_ingest_token, INGEST_TOKEN
    ):
        raise HTTPException(status_code=401, detail="ingest unauthorized")


def current_device(authorization: str | None = Header(default=None)) -> sqlite3.Row:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="device token required")
    raw = authorization.split(" ", 1)[1].strip()
    with db() as con:
        row = con.execute(
            "SELECT * FROM devices WHERE token_hash=? AND revoked=0",
            (token_hash(raw),),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="invalid device token")
        con.execute(
            "UPDATE devices SET last_seen_at=? WHERE id=?",
            (now_iso(), row["id"]),
        )
        con.commit()
        row = con.execute("SELECT * FROM devices WHERE id=?", (row["id"],)).fetchone()
    return row


def send_push(title: str, body: str, url: str = "/") -> int:
    if not (VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY):
        return 0
    payload = json.dumps({"title": title, "body": body, "url": url})
    sent = 0
    stale: list[int] = []
    with db() as con:
        rows = con.execute(
            """SELECT s.* FROM subscriptions s
               JOIN devices d ON d.id=s.device_id
               WHERE d.revoked=0"""
        ).fetchall()
        for row in rows:
            try:
                webpush(
                    subscription_info={
                        "endpoint": row["endpoint"],
                        "keys": {
                            "p256dh": row["p256dh"],
                            "auth": row["auth"],
                        },
                    },
                    data=payload,
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={"sub": VAPID_SUBJECT},
                    ttl=300,
                    timeout=15,
                )
                sent += 1
            except WebPushException as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (404, 410):
                    stale.append(row["id"])
            except Exception:
                pass
        for sid in stale:
            con.execute("DELETE FROM subscriptions WHERE id=?", (sid,))
        con.commit()
    return sent


app = FastAPI(title="Monções Alertas", version="1.0")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/health")
def health():
    return {"ok": True, "service": "moncoes-alertas"}


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return FileResponse(STATIC / "sw.js", media_type="application/javascript")


@app.get("/icon.svg")
def icon():
    return FileResponse(STATIC / "icon.svg", media_type="image/svg+xml")


@app.get("/api/config")
def config():
    return {"vapid_public_key": VAPID_PUBLIC_KEY}


@app.post("/api/register")
def register(body: RegisterBody):
    if not DEVICE_INVITE_CODE or not hmac.compare_digest(
        body.invite_code.strip(), DEVICE_INVITE_CODE
    ):
        raise HTTPException(status_code=403, detail="código de convite inválido")

    raw = secrets.token_urlsafe(32)
    with db() as con:
        cur = con.execute(
            "INSERT INTO devices(name,token_hash,created_at,last_seen_at) VALUES(?,?,?,?)",
            (body.device_name.strip(), token_hash(raw), now_iso(), now_iso()),
        )
        con.commit()
        device_id = cur.lastrowid
    return {"token": raw, "device_id": device_id, "device_name": body.device_name.strip()}


@app.get("/api/me")
def me(device=Depends(current_device)):
    with db() as con:
        push_count = con.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE device_id=?",
            (device["id"],),
        ).fetchone()[0]
    return {
        "id": device["id"],
        "name": device["name"],
        "push_subscribed": push_count > 0,
        "push_subscription_count": push_count,
    }


@app.post("/api/push/subscribe")
def subscribe(body: SubscriptionBody, device=Depends(current_device)):
    p256dh = body.keys.get("p256dh", "")
    auth = body.keys.get("auth", "")
    if not body.endpoint or not p256dh or not auth:
        raise HTTPException(status_code=400, detail="subscription incompleta")
    with db() as con:
        con.execute(
            """INSERT INTO subscriptions(device_id,endpoint,p256dh,auth,created_at,updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(endpoint) DO UPDATE SET
                 device_id=excluded.device_id,
                 p256dh=excluded.p256dh,
                 auth=excluded.auth,
                 updated_at=excluded.updated_at""",
            (device["id"], body.endpoint, p256dh, auth, now_iso(), now_iso()),
        )
        con.commit()
    return {"ok": True}


@app.post("/api/push/test")
def test_push(device=Depends(current_device)):
    if not (VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY):
        raise HTTPException(status_code=503, detail="Web Push não configurado")

    with db() as con:
        rows = con.execute(
            "SELECT * FROM subscriptions WHERE device_id=?",
            (device["id"],),
        ).fetchall()

    if not rows:
        raise HTTPException(
            status_code=409,
            detail="Este aparelho ainda não possui assinatura push registrada.",
        )

    payload = json.dumps(
        {
            "title": "Teste — Monções Alertas",
            "body": "Notificações do Monções Alertas estão funcionando.",
            "url": "/",
        }
    )
    sent = 0
    failures = []
    stale = []

    for row in rows:
        try:
            webpush(
                subscription_info={
                    "endpoint": row["endpoint"],
                    "keys": {
                        "p256dh": row["p256dh"],
                        "auth": row["auth"],
                    },
                },
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
                ttl=300,
                timeout=15,
            )
            sent += 1
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            failures.append(
                {"type": "WebPushException", "http_status": status}
            )
            if status in (404, 410):
                stale.append(row["id"])
        except Exception as exc:
            failures.append({"type": type(exc).__name__, "http_status": None})

    if stale:
        with db() as con:
            for sid in stale:
                con.execute("DELETE FROM subscriptions WHERE id=?", (sid,))
            con.commit()

    if sent == 0:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "O servidor não conseguiu entregar o push.",
                "failures": failures,
            },
        )

    return {"ok": True, "sent": sent, "failures": failures}


@app.get("/api/status")
def status(device=Depends(current_device)):
    with db() as con:
        row = con.execute("SELECT * FROM health WHERE singleton=1").fetchone()
    if not row:
        return {"received_at": None, "payload": None}
    return {"received_at": row["received_at"], "payload": json.loads(row["payload"])}


@app.get("/api/events")
def events(limit: int = 50, device=Depends(current_device)):
    limit = max(1, min(limit, 100))
    with db() as con:
        rows = con.execute(
            """SELECT e.*, d.name AS acknowledged_by_name
               FROM events e
               LEFT JOIN devices d ON d.id=e.acknowledged_by
               ORDER BY COALESCE(occurred_at,received_at) DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/events/{event_id}/ack")
def acknowledge(event_id: int, device=Depends(current_device)):
    with db() as con:
        cur = con.execute(
            """UPDATE events SET acknowledged_at=?, acknowledged_by=?
               WHERE id=?""",
            (now_iso(), device["id"], event_id),
        )
        con.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="evento não encontrado")
    return {"ok": True}


@app.get("/api/reports")
def report_list(device=Depends(current_device)):
    with db() as con:
        rows = con.execute(
            "SELECT * FROM reports ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
    return [dict(row) for row in rows]


def _report_signature(report_id: int, expires: int) -> str:
    if not INGEST_TOKEN:
        raise HTTPException(status_code=503, detail="assinatura de download indisponível")
    message = f"report:{report_id}:{expires}".encode("utf-8")
    return hmac.new(
        INGEST_TOKEN.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()


@app.post("/api/reports/{report_id}/link")
def report_link(report_id: int, request: Request, device=Depends(current_device)):
    with db() as con:
        row = con.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="relatório não encontrado")

    path = REPORTS / row["filename"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="arquivo indisponível")

    expires = int(datetime.now(timezone.utc).timestamp()) + 300
    sig = _report_signature(report_id, expires)
    url = request.url_for("report_download_signed").include_query_params(
        exp=expires,
        sig=sig,
    )
    return {"url": str(url), "expires_in_seconds": 300}


@app.get("/r/{report_id}", name="report_download_signed")
def report_download_signed(report_id: int, exp: int, sig: str):
    now_ts = int(datetime.now(timezone.utc).timestamp())
    if exp < now_ts or exp > now_ts + 600:
        raise HTTPException(status_code=403, detail="link expirado")

    expected = _report_signature(report_id, exp)
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=403, detail="assinatura inválida")

    with db() as con:
        row = con.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="relatório não encontrado")

    path = REPORTS / row["filename"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="arquivo indisponível")

    return FileResponse(
        path,
        media_type="application/pdf",
        filename=row["filename"],
    )


@app.post("/api/ingest/event")
def ingest_event(body: IngestEvent, _: None = Depends(require_ingest)):
    with db() as con:
        existing = con.execute(
            "SELECT id,status FROM events WHERE event_key=?", (body.event_key,)
        ).fetchone()
        if existing:
            return {"ok": True, "id": existing["id"], "duplicate": True, "push_sent": 0}

        cur = con.execute(
            """INSERT INTO events(
               event_key,received_at,occurred_at,dvr,channel,camera,status,confidence,
               category,description,rule_reference,needs_human_review,source
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                body.event_key, now_iso(), body.occurred_at, body.dvr, body.channel,
                body.camera, body.status, body.confidence, body.category,
                body.description, body.rule_reference,
                1 if body.needs_human_review else 0, body.source,
            ),
        )
        con.commit()
        event_id = cur.lastrowid

    push_sent = 0
    if body.status == "critical" and body.notify_external:
        location = body.camera or "área monitorada"
        push_sent = send_push(
            "⚠️ Alerta crítico — Monções",
            f"Alerta crítico detectado em {location}. Toque para abrir os detalhes.",
            f"/?event={event_id}",
        )
    return {"ok": True, "id": event_id, "duplicate": False, "push_sent": push_sent}


@app.post("/api/ingest/health")
def ingest_health(body: HealthBody, _: None = Depends(require_ingest)):
    with db() as con:
        con.execute(
            """INSERT INTO health(singleton,received_at,payload) VALUES(1,?,?)
               ON CONFLICT(singleton) DO UPDATE SET
                 received_at=excluded.received_at,payload=excluded.payload""",
            (now_iso(), json.dumps(body.payload)),
        )
        con.commit()
    return {"ok": True}


@app.post("/api/ingest/report")
async def ingest_report(
    kind: str,
    file: UploadFile = File(...),
    x_ingest_token: str | None = Header(default=None),
):
    require_ingest(x_ingest_token)
    if kind not in {"daily", "weekly"}:
        raise HTTPException(status_code=400, detail="kind inválido")
    safe_name = Path(file.filename or f"relatorio_{kind}.pdf").name
    if not safe_name.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="somente PDF")
    content = await file.read()
    if len(content) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="arquivo muito grande")
    target = REPORTS / safe_name
    target.write_bytes(content)
    with db() as con:
        cur = con.execute(
            """INSERT OR IGNORE INTO reports(kind,filename,created_at)
               VALUES(?,?,?)""",
            (kind, safe_name, now_iso()),
        )
        con.commit()
        row = con.execute(
            "SELECT id FROM reports WHERE kind=? AND filename=?",
            (kind, safe_name),
        ).fetchone()
    return {"ok": True, "id": row["id"] if row else cur.lastrowid}
