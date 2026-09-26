from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import threading
import time
from datetime import datetime, timedelta

import requests
from requests.auth import HTTPDigestAuth

from ai import analyze
from common import (
    CONFIG_DIR,
    EVENT_DIR,
    EVIDENCE_DIR,
    capture_live_frames,
    capture_playback_frames,
    db,
    get_state,
    load_json,
    make_contact_sheet,
    now_local,
    safe_remove,
    set_state,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("moncoes-monitor")

STOP = threading.Event()
CAMERAS = load_json(CONFIG_DIR / "cameras.json")
USER = os.getenv("DVR_USER", "admin")
PASSWORD = os.getenv("DVR_PASS", "")
DEBOUNCE = int(os.getenv("EVENT_DEBOUNCE_SECONDS", "90"))
POLL_SECONDS = int(os.getenv("DVR101_POLL_SECONDS", "300"))
MODE = os.getenv("MONCOES_MODE", "observe")
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "").strip()

last_event: dict[tuple[str, int], float] = {}
processing: set[tuple[str, int]] = set()
lock = threading.Lock()


def camera(dvr: str, channel: int):
    return CAMERAS.get(dvr, {}).get("channels", {}).get(str(channel))


def event_key(dvr: str, channel: int, start: datetime) -> str:
    return f"{dvr}:{channel}:{start.strftime('%Y%m%d%H%M%S')}"


def save_record(
    key: str,
    dvr: str,
    channel: int,
    cam: dict,
    start: datetime,
    end: datetime | None,
    source: str,
    triage,
    final,
    evidence: str | None,
    error: str | None = None,
):
    result = final or triage
    with db() as con:
        con.execute(
            """INSERT OR IGNORE INTO events(
                event_key,created_at,dvr,channel,camera_name,started_at,ended_at,source,
                priority,triage_status,final_status,confidence,category,description,
                rule_reference,needs_human_review,evidence_path,ai_model,review_model,error
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key,
                now_local().isoformat(),
                dvr,
                channel,
                cam["name"],
                start.isoformat(),
                end.isoformat() if end else None,
                source,
                cam.get("priority"),
                getattr(triage, "status", None),
                getattr(result, "status", None),
                getattr(result, "confidence", None),
                getattr(result, "category", None),
                getattr(result, "description", None),
                getattr(result, "rule_reference", None),
                1 if getattr(result, "needs_human_review", False) else 0,
                evidence,
                os.getenv("OPENAI_MODEL", "gpt-5.6-luna"),
                os.getenv("OPENAI_REVIEW_MODEL", "gpt-5.6-terra"),
                error,
            ),
        )
        con.commit()


def notify_if_critical(payload: dict):
    if not ALERT_WEBHOOK_URL or MODE != "production":
        return
    try:
        requests.post(ALERT_WEBHOOK_URL, json=payload, timeout=10).raise_for_status()
    except Exception as exc:
        log.error("Falha no webhook de alerta: %s", exc)


def process_event(
    dvr: str,
    channel: int,
    start: datetime,
    end: datetime | None = None,
    source: str = "realtime",
):
    cam = camera(dvr, channel)
    if not cam or not cam.get("enabled", False):
        return

    key = event_key(dvr, channel, start)
    with db() as con:
        if con.execute("SELECT 1 FROM events WHERE event_key=?", (key,)).fetchone():
            return

    host = CAMERAS[dvr]["host"]
    work = EVENT_DIR / start.strftime("%Y-%m-%d") / key.replace(":", "_")
    sheet = work / "contact.jpg"

    try:
        if source == "realtime":
            frames = capture_live_frames(
                host,
                USER,
                PASSWORD,
                channel,
                work,
                seconds=12,
                count=4,
                rotate=int(cam.get("rotate", 0)),
            )
        else:
            frames = capture_playback_frames(
                host,
                USER,
                PASSWORD,
                channel,
                start,
                end or start + timedelta(seconds=20),
                work,
                count=4,
                rotate=int(cam.get("rotate", 0)),
            )

        make_contact_sheet(frames, sheet)
        triage = analyze(sheet, cam, dvr, channel, "triage")
        final = triage

        if (
            triage.status in {"potential_occurrence", "critical", "uncertain"}
            and os.getenv("OPENAI_API_KEY", "").strip()
        ):
            try:
                final = analyze(sheet, cam, dvr, channel, "review")
            except Exception as exc:
                log.warning("Segunda revisão IA falhou; usando triagem: %s", exc)

        evidence_path = None
        if final.status in {"potential_occurrence", "critical", "uncertain"}:
            destination = (
                EVIDENCE_DIR
                / start.strftime("%Y-%m-%d")
                / f"{key.replace(':', '_')}.jpg"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sheet, destination)
            evidence_path = str(destination)

        save_record(
            key, dvr, channel, cam, start, end, source,
            triage, final, evidence_path
        )
        log.info(
            "Evento %s camera=%s status=%s confidence=%.2f",
            key, cam["name"], final.status, final.confidence
        )

        if final.status == "critical":
            notify_if_critical(
                {
                    "event_key": key,
                    "dvr": dvr,
                    "channel": channel,
                    "camera": cam["name"],
                    "status": final.status,
                    "confidence": final.confidence,
                    "description": final.description,
                    "evidence_path": evidence_path,
                }
            )

    except Exception as exc:
        log.exception("Falha processando %s", key)
        save_record(
            key, dvr, channel, cam, start, end, source,
            None, None, None, str(exc)
        )
    finally:
        safe_remove(work)
        with lock:
            processing.discard((dvr, channel))


def schedule_realtime(dvr: str, index: int):
    channel = index + 1
    cam = camera(dvr, channel)
    if not cam or not cam.get("enabled", False):
        return

    current = time.time()
    key = (dvr, channel)
    with lock:
        if key in processing:
            return
        if current - last_event.get(key, 0) < DEBOUNCE:
            return
        last_event[key] = current
        processing.add(key)

    threading.Thread(
        target=process_event,
        args=(dvr, channel, now_local(), None, "realtime"),
        daemon=True,
    ).start()


def listener_dvr100():
    host = CAMERAS["100"]["host"]
    url = (
        f"http://{host}/cgi-bin/eventManager.cgi"
        "?action=attach&codes=[VideoMotion]&heartbeat=5"
    )
    auth = HTTPDigestAuth(USER, PASSWORD)
    backoff = 2

    while not STOP.is_set():
        try:
            log.info("Conectando listener do DVR100")
            with requests.get(
                url,
                auth=auth,
                stream=True,
                timeout=(10, 90),
            ) as response:
                response.raise_for_status()
                backoff = 2
                for raw in response.iter_lines(decode_unicode=True):
                    if STOP.is_set():
                        break
                    line = (raw or "").strip()
                    match = re.search(
                        r"Code=VideoMotion;action=Start;index=(\d+)",
                        line,
                    )
                    if match:
                        schedule_realtime("100", int(match.group(1)))
        except Exception as exc:
            log.warning(
                "Listener DVR100 caiu: %s; nova tentativa em %ss",
                exc,
                backoff,
            )
            STOP.wait(backoff)
            backoff = min(backoff * 2, 60)


def api_get(host: str, params: dict) -> str:
    url = f"http://{host}/cgi-bin/mediaFileFind.cgi"
    response = requests.get(
        url,
        params=params,
        auth=HTTPDigestAuth(USER, PASSWORD),
        timeout=30,
    )
    response.raise_for_status()
    return response.text


def search_motion(
    host: str,
    physical_channel: int,
    start: datetime,
    end: datetime,
):
    create_text = api_get(host, {"action": "factory.create"})
    match = re.search(r"result=(\d+)", create_text)
    if not match:
        return []

    obj = match.group(1)
    events: list[tuple[datetime, datetime]] = []

    try:
        result = api_get(
            host,
            {
                "action": "findFile",
                "object": obj,
                "condition.Channel": physical_channel,
                "condition.StartTime": start.strftime("%Y-%m-%d %H:%M:%S"),
                "condition.EndTime": end.strftime("%Y-%m-%d %H:%M:%S"),
                "condition.Types[0]": "dav",
                "condition.Flags[0]": "Event",
                "condition.Events[0]": "VideoMotion",
            },
        )
        if "OK" not in result:
            return []

        for _ in range(20):
            text = api_get(
                host,
                {"action": "findNextFile", "object": obj, "count": 100},
            )
            found_match = re.search(r"found=(\d+)", text)
            found = int(found_match.group(1)) if found_match else 0

            starts = {
                int(i): value.strip()
                for i, value in re.findall(
                    r"items\[(\d+)\]\.StartTime=(.+)", text
                )
            }
            ends = {
                int(i): value.strip()
                for i, value in re.findall(
                    r"items\[(\d+)\]\.EndTime=(.+)", text
                )
            }

            for idx in sorted(set(starts) & set(ends)):
                try:
                    start_dt = datetime.strptime(
                        starts[idx], "%Y-%m-%d %H:%M:%S"
                    ).astimezone()
                    end_dt = datetime.strptime(
                        ends[idx], "%Y-%m-%d %H:%M:%S"
                    ).astimezone()
                    events.append((start_dt, end_dt))
                except ValueError:
                    pass

            if found < 100:
                break
    finally:
        try:
            api_get(host, {"action": "close", "object": obj})
            api_get(host, {"action": "destroy", "object": obj})
        except Exception:
            pass

    events.sort()
    merged: list[tuple[datetime, datetime]] = []
    for start_dt, end_dt in events:
        if (
            merged
            and start_dt <= merged[-1][1] + timedelta(seconds=DEBOUNCE)
        ):
            merged[-1] = (
                merged[-1][0],
                max(merged[-1][1], end_dt),
            )
        else:
            merged.append((start_dt, end_dt))

    return merged


def poll_dvr101():
    host = CAMERAS["101"]["host"]

    while not STOP.is_set():
        try:
            now = now_local()
            previous = get_state("dvr101_last_poll", "")
            start = (
                datetime.fromisoformat(previous)
                if previous
                else now - timedelta(minutes=5)
            )
            start = max(
                start - timedelta(seconds=10),
                now - timedelta(minutes=15),
            )

            for channel_text, cam in CAMERAS["101"]["channels"].items():
                if not cam.get("enabled", False):
                    continue
                channel = int(channel_text)
                for event_start, event_end in search_motion(
                    host, channel, start, now
                ):
                    process_event(
                        "101",
                        channel,
                        event_start,
                        event_end,
                        "poll",
                    )

            set_state("dvr101_last_poll", now.isoformat())
        except Exception as exc:
            log.exception("Polling DVR101 falhou: %s", exc)

        STOP.wait(POLL_SECONDS)


def handle_signal(*_):
    STOP.set()


if __name__ == "__main__":
    if not PASSWORD:
        raise SystemExit("DVR_PASS ausente")

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    threads = [
        threading.Thread(target=listener_dvr100, daemon=True),
        threading.Thread(target=poll_dvr101, daemon=True),
    ]
    for thread in threads:
        thread.start()

    log.info("Monções AI Monitor iniciado em modo=%s", MODE)
    while not STOP.wait(5):
        pass
    log.info("Encerrando Monções AI Monitor")
