"""File-based kill switch. The HALT file's existence is the truth — checks are cheap."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from kalshi_agent.journal.logger import get_logger

UTC = timezone.utc

log = get_logger(__name__)


class KillSwitch:
    def __init__(self, halt_file_path: Path) -> None:
        self._path = halt_file_path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def is_engaged(self) -> bool:
        return self._path.exists()

    def reason(self) -> dict | None:
        if not self.is_engaged():
            return None
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return {"reason": "manual", "timestamp": None}

    def engage(self, reason: str, *, source: str = "manual", payload: dict | None = None) -> None:
        if self.is_engaged():
            log.info("kill_switch_already_engaged", reason=reason, source=source)
            return
        body = {
            "reason": reason,
            "source": source,
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": payload or {},
        }
        # Atomic write: tmp then rename
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, indent=2))
        os.replace(tmp, self._path)
        log.warning("kill_switch_engaged", **body)

    def disengage(self) -> bool:
        if not self.is_engaged():
            return False
        try:
            self._path.unlink()
            log.warning("kill_switch_disengaged")
            return True
        except FileNotFoundError:
            return False
