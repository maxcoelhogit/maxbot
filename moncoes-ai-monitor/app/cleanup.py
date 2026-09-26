from __future__ import annotations

import os
import time

from common import EVENT_DIR, EVIDENCE_DIR, REPORT_DIR, TMP_DIR

now = time.time()
policies = [
    (EVENT_DIR, 2),
    (TMP_DIR, 2),
    (EVIDENCE_DIR, int(os.getenv("EVIDENCE_RETENTION_DAYS", "30"))),
    (REPORT_DIR, int(os.getenv("REPORT_RETENTION_DAYS", "365"))),
]

for root, days in policies:
    if not root.exists():
        continue
    for path in sorted(root.rglob("*"), reverse=True):
        try:
            if path.is_file() and now - path.stat().st_mtime > days * 86400:
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        except Exception:
            pass
