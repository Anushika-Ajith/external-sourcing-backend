"""Performance instrumentation for sales transaction upload."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager, nullcontext
from typing import Dict, Iterator, Optional

logger = logging.getLogger(__name__)

LOG_PREFIX = "[SALES-UPLOAD-PERF]"


def perf_stage(perf: Optional["SalesUploadPerf"], name: str):
    if perf is not None:
        return perf.stage(name)
    return nullcontext()


class SalesUploadPerf:
    """Collect stage timings for a single sales upload request."""

    def __init__(self, *, label: str = "Sales Upload Performance") -> None:
        self.label = label
        self._started = time.perf_counter()
        self._stages: Dict[str, float] = {}

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        self._emit(f"START {name}")
        try:
            yield
        finally:
            elapsed = time.perf_counter() - start
            self._stages[name] = self._stages.get(name, 0.0) + elapsed
            self._emit(f"DONE  {name}: {elapsed:.3f}s")

    def add(self, name: str, seconds: float) -> None:
        self._stages[name] = self._stages.get(name, 0.0) + max(0.0, seconds)
        self._emit(f"ADD   {name}: {seconds:.3f}s")

    @property
    def total_seconds(self) -> float:
        return time.perf_counter() - self._started

    def as_dict(self) -> Dict[str, float]:
        out = dict(sorted(self._stages.items(), key=lambda item: item[1], reverse=True))
        out["total"] = round(self.total_seconds, 3)
        return {key: round(value, 3) for key, value in out.items()}

    def _emit(self, message: str) -> None:
        line = f"{LOG_PREFIX} {message}"
        print(line, flush=True)
        logger.info(line)

    def log_summary(self, *, extra: Optional[Dict[str, object]] = None) -> None:
        total = self.total_seconds
        ranked = sorted(self._stages.items(), key=lambda item: item[1], reverse=True)

        lines = [f"{LOG_PREFIX} === {self.label} ==="]
        if not ranked:
            lines.append(f"{LOG_PREFIX} (no stages recorded)")
        else:
            slowest_name, slowest_seconds = ranked[0]
            for name, seconds in ranked:
                pct = (seconds / total * 100.0) if total > 0 else 0.0
                marker = "  ← slowest" if name == slowest_name and len(ranked) > 1 else ""
                lines.append(
                    f"{LOG_PREFIX}   {name}: {seconds:.3f}s ({pct:.1f}%){marker}"
                )
        lines.append(f"{LOG_PREFIX}   TOTAL: {total:.3f}s")
        if extra:
            safe_extra = {
                k: v
                for k, v in extra.items()
                if k not in {"raw_text", "lines"} and v is not None
            }
            if safe_extra:
                lines.append(f"{LOG_PREFIX}   context: {safe_extra}")

        block = "\n".join(lines)
        print(block, flush=True)
        logger.info(block)
