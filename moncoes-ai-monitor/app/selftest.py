from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime

from ai import analyze
from common import CONFIG_DIR, TMP_DIR, capture_live_frames, load_json, make_contact_sheet

CAMERAS = load_json(CONFIG_DIR / "cameras.json")


def main() -> int:
    dvr = "100"
    channel = 3
    camera = CAMERAS[dvr]["channels"][str(channel)]
    host = CAMERAS[dvr]["host"]

    import os
    user = os.getenv("DVR_USER", "admin")
    password = os.getenv("DVR_PASS", "")
    if not password:
        print(json.dumps({"ok": False, "stage": "config", "error": "DVR_PASS ausente"}, ensure_ascii=False))
        return 2
    if not os.getenv("OPENAI_API_KEY", "").strip():
        print(json.dumps({"ok": False, "stage": "config", "error": "OPENAI_API_KEY ausente"}, ensure_ascii=False))
        return 2

    work = TMP_DIR / f"selftest-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    sheet = work / "contact.jpg"

    try:
        print("1/3 Capturando 4 frames ao vivo da câmera 3 do DVR100...", flush=True)
        frames = capture_live_frames(
            host, user, password, channel, work,
            seconds=8, count=4, rotate=int(camera.get("rotate", 0)),
        )
        print(f"2/3 Frames capturados: {len(frames)}. Montando contato...", flush=True)
        make_contact_sheet(frames, sheet)
        print("3/3 Enviando teste de visão à OpenAI (sem gravar ocorrência)...", flush=True)
        result = analyze(sheet, camera, dvr, channel, "triage")
        print(json.dumps({
            "ok": True,
            "dvr": dvr,
            "channel": channel,
            "camera": camera["name"],
            "status": result.status,
            "category": result.category,
            "confidence": result.confidence,
            "description": result.description,
            "rule_reference": result.rule_reference,
            "needs_human_review": result.needs_human_review,
            "note": "SELFTEST: não salvo no banco de ocorrências e não gera alerta.",
        }, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "stage": "runtime", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
