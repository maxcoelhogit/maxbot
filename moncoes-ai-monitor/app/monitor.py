from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode

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
    record_prefilter_result,
    safe_remove,
    set_state,
)
from prefilter import inspect_frames

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
SECURITY_DEBOUNCE = int(os.getenv("SECURITY_EVENT_DEBOUNCE_SECONDS", "20"))
POLL_MERGE_SECONDS = int(os.getenv("POLL_MERGE_SECONDS", "20"))
POLL_SECONDS = int(os.getenv("DVR101_POLL_SECONDS", "300"))
MODE = os.getenv("MONCOES_MODE", "observe")
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "").strip()
PORTAL_BASE_URL = os.getenv("PORTAL_BASE_URL", "").strip().rstrip("/")
PORTAL_INGEST_TOKEN = os.getenv("PORTAL_INGEST_TOKEN", "").strip()

last_event: dict[tuple[str, int], float] = {}
processing: set[tuple[str, int]] = set()
lock = threading.Lock()


def camera(dvr: str, channel: int):
    return CAMERAS.get(dvr, {}).get("channels", {}).get(str(channel))


def event_key(dvr: str, channel: int, start: datetime) -> str:
    return f"{dvr}:{channel}:{start.strftime('%Y%m%d%H%M%S')}"


def event_exists_nearby(
    dvr: str,
    channel: int,
    start: datetime,
    tolerance_seconds: int = SECURITY_DEBOUNCE,
) -> bool:
    lower = (start - timedelta(seconds=tolerance_seconds)).isoformat()
    upper = (start + timedelta(seconds=tolerance_seconds)).isoformat()
    with db() as con:
        row = con.execute(
            """SELECT 1 FROM events
               WHERE dvr=? AND channel=? AND error IS NULL
                 AND started_at>=? AND started_at<=?
               LIMIT 1""",
            (dvr, channel, lower, upper),
        ).fetchone()
    return bool(row)


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


def publish_portal_event(payload: dict):
    if not PORTAL_BASE_URL or not PORTAL_INGEST_TOKEN:
        return
    try:
        requests.post(
            f"{PORTAL_BASE_URL}/api/ingest/event",
            json=payload,
            headers={"X-Ingest-Token": PORTAL_INGEST_TOKEN},
            timeout=8,
        ).raise_for_status()
    except Exception as exc:
        log.warning("Falha sincronizando evento com portal: %s", exc)


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
    if event_exists_nearby(dvr, channel, start):
        return

    host = CAMERAS[dvr]["host"]
    work = EVENT_DIR / start.strftime("%Y-%m-%d") / key.replace(":", "_")
    sheet = work / "contact.jpg"

    try:
        if source == "realtime":
            live_seconds = int(cam.get("capture_seconds", 12))
            frames = capture_live_frames(
                host,
                USER,
                PASSWORD,
                channel,
                work,
                seconds=live_seconds,
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

        decision = inspect_frames(
            frames,
            cam,
            dvr=dvr,
            channel=channel,
        )
        record_prefilter_result(
            dvr,
            channel,
            passed=decision.should_analyze,
            fail_open=decision.fail_open,
        )

        if not decision.should_analyze:
            log.info(
                "Pré-filtro local descartou evento %s camera=%s motivo=%s",
                key,
                cam["name"],
                decision.reason,
            )
            return

        log.info(
            "Pré-filtro liberou evento %s camera=%s labels=%s conf=%.2f motivo=%s",
            key,
            cam["name"],
            ",".join(decision.labels) if decision.labels else "-",
            decision.max_confidence,
            decision.reason,
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
        if (
            final.status in {"potential_occurrence", "critical", "uncertain"}
            and final.category != "ai_disabled"
        ):
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

        publish_portal_event(
            {
                "event_key": key,
                "occurred_at": start.isoformat(),
                "dvr": dvr,
                "channel": channel,
                "camera": cam["name"],
                "status": final.status,
                "confidence": final.confidence,
                "category": final.category,
                "description": final.description,
                "rule_reference": final.rule_reference,
                "needs_human_review": final.needs_human_review,
                "source": source,
                "notify_external": MODE == "production",
            }
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
        if current - last_event.get(key, 0) < SECURITY_DEBOUNCE:
            return
        last_event[key] = current
        processing.add(key)

    threading.Thread(
        target=process_event,
        args=(dvr, channel, now_local(), None, "realtime"),
        daemon=True,
    ).start()


def schedule_listener_stop(dvr: str, index: int):
    """Fallback para firmwares que notificam apenas VideoMotion Stop.

    Usa playback dos segundos anteriores ao Stop, preservando a evidência
    do evento em vez de capturar apenas o cenário já encerrado.
    """
    channel = index + 1
    cam = camera(dvr, channel)
    if not cam or not cam.get("enabled", False):
        return

    current = time.time()
    key = (dvr, channel)
    with lock:
        if key in processing:
            return
        if current - last_event.get(key, 0) < SECURITY_DEBOUNCE:
            return
        last_event[key] = current
        processing.add(key)

    end = now_local()
    start = end - timedelta(seconds=20)

    def delayed():
        # Aguarda o DVR finalizar/indexar o arquivo e usa os tempos reais
        # retornados pelo mediaFileFind. Alguns firmwares retornam 404 quando
        # playback é solicitado com uma janela arbitrária.
        if STOP.wait(8):
            with lock:
                processing.discard(key)
            return

        try:
            host = CAMERAS[dvr]["host"]
            candidates = search_motion(
                host,
                channel,
                end - timedelta(seconds=90),
                end + timedelta(seconds=5),
            )
            if candidates:
                actual_start, actual_end = candidates[-1]
                # process_event fará o discard de processing no finally.
                process_event(
                    dvr,
                    channel,
                    actual_start,
                    actual_end,
                    "listener_stop",
                )
                return

            log.info(
                "VideoMotion Stop DVR%s cam%s sem playback indexado; "
                "reconciliação periódica fará nova tentativa",
                dvr,
                channel,
            )
        except Exception as exc:
            log.warning(
                "Fallback VideoMotion Stop DVR%s cam%s falhou: %s",
                dvr,
                channel,
                exc,
            )

        with lock:
            processing.discard(key)

    threading.Thread(target=delayed, daemon=True).start()


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
                for raw in response.iter_lines(decode_unicode=False):
                    if STOP.is_set():
                        break
                    if isinstance(raw, bytes):
                        line = raw.decode("utf-8", errors="ignore").strip()
                    else:
                        line = str(raw or "").strip()
                    match = re.search(
                        r"Code=VideoMotion;action=(Start|Stop);index=(\d+)",
                        line,
                    )
                    if match:
                        action = match.group(1)
                        index = int(match.group(2))
                        if action == "Start":
                            schedule_realtime("100", index)
                        else:
                            schedule_listener_stop("100", index)
        except Exception as exc:
            log.warning(
                "Listener DVR100 caiu: %s; nova tentativa em %ss",
                exc,
                backoff,
            )
            STOP.wait(backoff)
            backoff = min(backoff * 2, 60)


def api_get(host: str, params: dict) -> str:
    # Alguns firmwares Intelbras/Dahua rejeitam '+' como separador de espaço
    # nos horários do mediaFileFind. Forçamos percent-encoding (%20), igual
    # à sintaxe que já foi validada diretamente neste DVR.
    base_url = f"http://{host}/cgi-bin/mediaFileFind.cgi"
    query = urlencode(params, doseq=True, quote_via=quote)
    response = requests.get(
        f"{base_url}?{query}",
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
        except requests.HTTPError as exc:
            # Estes DVRs retornam HTTP 400 quando a janela/canal não tem
            # arquivos de movimento correspondentes. Isso não é falha do
            # monitor; a próxima janela será consultada normalmente.
            if exc.response is not None and exc.response.status_code == 400:
                return []
            raise
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
            and start_dt <= merged[-1][1] + timedelta(seconds=POLL_MERGE_SECONDS)
        ):
            merged[-1] = (
                merged[-1][0],
                max(merged[-1][1], end_dt),
            )
        else:
            merged.append((start_dt, end_dt))

    return merged


def poll_dvr(dvr: str):
    host = CAMERAS[dvr]["host"]
    state_key = f"dvr{dvr}_last_poll"

    while not STOP.is_set():
        now = now_local()
        previous = get_state(state_key, "")
        start = (
            datetime.fromisoformat(previous)
            if previous
            else now - timedelta(minutes=10)
        )
        start = max(
            start - timedelta(seconds=SECURITY_DEBOUNCE),
            now - timedelta(minutes=20),
        )

        found_total = 0
        for channel_text, cam in CAMERAS[dvr]["channels"].items():
            if not cam.get("enabled", False):
                continue
            channel = int(channel_text)
            try:
                events = search_motion(host, channel, start, now)
                found_total += len(events)
                for event_start, event_end in events:
                    process_event(
                        dvr,
                        channel,
                        event_start,
                        event_end,
                        "poll",
                    )
            except Exception as exc:
                log.warning(
                    "Reconciliação DVR%s cam%s falhou: %s",
                    dvr,
                    channel,
                    exc,
                )

        set_state(state_key, now.isoformat())
        if found_total:
            log.info(
                "Reconciliação DVR%s encontrou %s grupo(s) de movimento",
                dvr,
                found_total,
            )

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
        threading.Thread(target=poll_dvr, args=("100",), daemon=True),
        threading.Thread(target=poll_dvr, args=("101",), daemon=True),
    ]
    for thread in threads:
        thread.start()

    log.info("Monções AI Monitor iniciado em modo=%s", MODE)
    while not STOP.wait(5):
        pass
    log.info("Encerrando Monções AI Monitor")
