from __future__ import annotations

import json
import logging
import math
import time
from collections import defaultdict, deque


class Metrics:
    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self.timeline: deque[dict] = deque(maxlen=256)
        self.samples: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=256))

    def mark(self, stage: str, *, at: float | None = None, **labels) -> float:
        now = time.monotonic() if at is None else at
        row = {"stage": stage, "monotonic_seconds": now, **labels}
        self.timeline.append(row)
        if self.enabled:
            logging.getLogger("oopz_mcp.metrics").info(json.dumps(row, ensure_ascii=False))
        return now

    def duration(self, name: str, start: float, end: float) -> None:
        self.samples[name].append(max(0.0, (end - start) * 1000))

    def snapshot(self) -> dict:
        values = {}
        for name, samples in self.samples.items():
            ordered = sorted(samples)
            if ordered:
                values[name] = {
                    "samples": len(ordered),
                    "p50_ms": ordered[math.ceil(len(ordered) * 0.50) - 1],
                    "p95_ms": ordered[math.ceil(len(ordered) * 0.95) - 1],
                }
        return {
            "clock": "server_monotonic",
            "scope": "server_only_not_end_to_end",
            "window_samples": 256,
            "durations": values,
            "recent_stages": list(self.timeline)[-32:],
        }
