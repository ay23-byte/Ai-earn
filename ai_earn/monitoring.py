"""Health monitoring — periodic checks, structured logs, no secrets.

Collects health payloads over time and emits structured logs. Never exposes
DB over network; monitoring is local read-only. Future HTTP /healthz would
bind 127.0.0.1 and return 200 vs 503 based on payload.ok.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Mapping

from .health import check, check_with_backups
from .structured_logging import get_logger

@dataclass
class HealthSample:
    at: float
    ok: bool
    payload: dict

@dataclass
class Monitor:
    environ: Mapping[str, str] = field(default_factory=dict)
    samples: list[HealthSample] = field(default_factory=list)
    max_samples: int = 100

    def __post_init__(self):
        if not self.environ:
            self.environ = dict(os.environ)

    def poll(self, with_backups: bool = False) -> HealthSample:
        fn = check_with_backups if with_backups else check
        payload = fn(self.environ)
        sample = HealthSample(at=time.time(), ok=bool(payload.get("ok")), payload=payload)
        self.samples.append(sample)
        if len(self.samples) > self.max_samples:
            self.samples.pop(0)
        # Structured log (INFO for ok, WARNING for not ok, ERROR for config invalid)
        logger = get_logger()
        checks = payload.get("checks", {})
        if checks.get("config", {}).get("status") == "invalid_configuration":
            logger.error("health_check", message="health not ok", fields={"ok": payload.get("ok"), "config": "invalid_configuration"})
        elif not payload.get("ok"):
            logger.warning("health_check", message="health not ok", fields={"ok": False, "auth": checks.get("auth", {}).get("status"), "state": checks.get("state", {}).get("status")})
        else:
            logger.info("health_check", message="health ok", fields={"ok": True})
        return sample

    def status(self) -> dict:
        if not self.samples:
            return {"ok": False, "samples": 0, "last": None}
        last = self.samples[-1]
        return {
            "ok": last.ok,
            "samples": len(self.samples),
            "last": last.payload,
            "uptime_samples": len([s for s in self.samples if s.ok]),
        }

    def to_json(self) -> str:
        return json.dumps(self.status(), indent=2, sort_keys=True)
