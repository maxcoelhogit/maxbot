from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

BASE = Path(os.getenv("MONCOES_BASE", "/opt/moncoes-ai"))
CONFIG_DIR = BASE / "config"
DATA_DIR = BASE / "data"
EVENT_DIR = DATA_DIR / "events"
EVIDENCE_DIR = DATA_DIR / "evidence"
REPORT_DIR = BASE / "reports"
TMP_DIR = BASE / "tmp"
DB_PATH = DATA_DIR / "moncoes.db"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def now_local() -> datetime:
    return datetime.now().astimezone()


def ensure_dirs() -> None:
    for path in (DATA_DIR, EVENT_DIR, EVIDENCE_DIR, REPORT_DIR, TMP_DIR):
        path.mkdir(parents=True, exist_ok=True)


def db() -> sqlite3.Connection:
    ensure_dirs()
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      event_key TEXT UNIQUE NOT NULL,
      created_at TEXT NOT NULL,
      dvr TEXT NOT NULL,
      channel INTEGER NOT NULL,
      camera_name TEXT NOT NULL,
      started_at TEXT NOT NULL,
      ended_at TEXT,
      source TEXT NOT NULL,
      priority TEXT,
      triage_status TEXT,
      final_status TEXT,
      confidence REAL,
      category TEXT,
      description TEXT,
      rule_reference TEXT,
      needs_human_review INTEGER DEFAULT 0,
      evidence_path TEXT,
      ai_model TEXT,
      review_model TEXT,
      error TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_events_started ON events(started_at);
    CREATE INDEX IF NOT EXISTS idx_events_status ON events(final_status);
    CREATE TABLE IF NOT EXISTS state (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS prefilter_stats (
      day TEXT NOT NULL,
      dvr TEXT NOT NULL,
      channel INTEGER NOT NULL,
      received INTEGER NOT NULL DEFAULT 0,
      passed INTEGER NOT NULL DEFAULT 0,
      skipped INTEGER NOT NULL DEFAULT 0,
      fail_open INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY(day,dvr,channel)
    );
    """)
    return con


def record_prefilter_result(
    dvr: str,
    channel: int,
    *,
    passed: bool,
    fail_open: bool = False,
) -> None:
    """Aggregate local-filter efficiency without storing discarded images."""
    day = now_local().date().isoformat()
    with db() as con:
        con.execute(
            """
            INSERT INTO prefilter_stats(
              day,dvr,channel,received,passed,skipped,fail_open
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(day,dvr,channel) DO UPDATE SET
              received=received+1,
              passed=passed+excluded.passed,
              skipped=skipped+excluded.skipped,
              fail_open=fail_open+excluded.fail_open
            """,
            (
                day,
                dvr,
                channel,
                1,
                1 if passed else 0,
                0 if passed else 1,
                1 if fail_open else 0,
            ),
        )
        con.commit()


def get_state(key: str, default: str = "") -> str:
    with db() as con:
        row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default


def set_state(key: str, value: str) -> None:
    with db() as con:
        con.execute(
            "INSERT INTO state(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        con.commit()


def run(cmd: list[str], timeout: int = 90, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=check,
    )


def redact_secrets(text: str, *secrets_to_hide: str) -> str:
    """Remove credenciais de mensagens antes de gravar log/banco."""
    value = text or ""
    for secret in secrets_to_hide:
        if secret:
            value = value.replace(secret, "***")
    value = re.sub(
        r"(rtsp://[^:/@\s]+:)[^@\s]+(@)",
        r"\1***\2",
        value,
        flags=re.IGNORECASE,
    )
    return value


def rtsp_url(host: str, user: str, password: str, channel: int, subtype: int = 0) -> str:
    return (
        f"rtsp://{user}:{password}@{host}:554/cam/realmonitor"
        f"?channel={channel}&subtype={subtype}"
    )


def playback_url(
    host: str,
    user: str,
    password: str,
    channel: int,
    start: datetime,
    end: datetime,
) -> str:
    return (
        f"rtsp://{user}:{password}@{host}:554/cam/playback?channel={channel}"
        f"&starttime={start.strftime('%Y_%m_%d_%H_%M_%S')}"
        f"&endtime={end.strftime('%Y_%m_%d_%H_%M_%S')}"
    )


def _video_filter(count: int, duration: float, rotate: int) -> str:
    vf = f"fps={count / max(duration, 1):.8f}"
    if rotate == 90:
        vf += ",transpose=1"
    elif rotate == 270:
        vf += ",transpose=2"
    elif rotate == 180:
        vf += ",hflip,vflip"
    return vf


def capture_live_frames(
    host: str,
    user: str,
    password: str,
    channel: int,
    out_dir: Path,
    seconds: int = 12,
    count: int = 4,
    rotate: int = 0,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-rtsp_transport", "tcp",
        "-i", rtsp_url(host, user, password, channel, 0),
        "-t", str(seconds),
        "-vf", _video_filter(count, float(seconds), rotate),
        "-frames:v", str(count),
        str(out_dir / "frame_%02d.jpg"),
    ]
    result = run(cmd, timeout=max(45, seconds + 30), check=False)
    frames = sorted(out_dir.glob("frame_*.jpg"))
    if result.returncode != 0 or not frames:
        raise RuntimeError(
            f"Falha FFmpeg live: {redact_secrets(result.stderr[-600:], password)}"
        )
    return frames


def capture_playback_frames(
    host: str,
    user: str,
    password: str,
    channel: int,
    start: datetime,
    end: datetime,
    out_dir: Path,
    count: int = 4,
    rotate: int = 0,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    max_end = min(end, start + timedelta(seconds=20))
    if max_end <= start:
        max_end = start + timedelta(seconds=8)
    duration = max(4.0, (max_end - start).total_seconds())
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-rtsp_transport", "tcp",
        "-i", playback_url(host, user, password, channel, start, max_end),
        "-vf", _video_filter(count, duration, rotate),
        "-frames:v", str(count),
        str(out_dir / "frame_%02d.jpg"),
    ]
    result = run(cmd, timeout=50, check=False)
    frames = sorted(out_dir.glob("frame_*.jpg"))
    if result.returncode != 0 or not frames:
        raise RuntimeError(
            f"Falha FFmpeg playback: {redact_secrets(result.stderr[-600:], password)}"
        )
    return frames


def make_contact_sheet(frames: list[Path], output: Path, target_width: int = 1280) -> Path:
    if not frames:
        raise RuntimeError("Nenhum frame disponível")
    source = [ImageOps.exif_transpose(Image.open(path).convert("RGB")) for path in frames[:4]]
    while len(source) < 4:
        source.append(source[-1].copy())

    tile_w = target_width // 2
    resized = []
    for image in source:
        ratio = tile_w / image.width
        resized.append(image.resize((tile_w, max(1, int(image.height * ratio)))))
    tile_h = min(image.height for image in resized)
    resized = [image.crop((0, 0, tile_w, tile_h)) for image in resized]

    sheet = Image.new("RGB", (tile_w * 2, tile_h * 2), "black")
    for idx, image in enumerate(resized):
        sheet.paste(image, ((idx % 2) * tile_w, (idx // 2) * tile_h))

    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output, quality=85, optimize=True)

    for image in source + resized:
        try:
            image.close()
        except Exception:
            pass
    sheet.close()
    return output


def data_url(path: Path) -> str:
    mime = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def safe_remove(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    except Exception:
        pass
