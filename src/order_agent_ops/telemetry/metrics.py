"""Thread-safe in-memory count, error, and duration aggregation."""

import math
from dataclasses import dataclass, field
from threading import RLock


@dataclass(frozen=True, slots=True)
class MetricSummary:
    name: str
    count: int
    error_count: int
    success_count: int
    success_rate: float
    error_rate: float
    p95_duration_ms: float | None


@dataclass(slots=True)
class _MetricSeries:
    count: int = 0
    error_count: int = 0
    durations_ms: list[float] = field(default_factory=list)


class MetricRegistry:
    """Aggregate local operation metrics without external dependencies."""

    def __init__(self) -> None:
        self._series: dict[str, _MetricSeries] = {}
        self._lock = RLock()

    def record(self, name: str, duration_ms: float, *, success: bool) -> None:
        if not name.strip():
            raise ValueError("Metric name must not be empty")
        if duration_ms < 0:
            raise ValueError("Metric duration must not be negative")

        with self._lock:
            series = self._series.setdefault(name, _MetricSeries())
            series.count += 1
            if not success:
                series.error_count += 1
            series.durations_ms.append(float(duration_ms))

    def summary(self, name: str) -> MetricSummary:
        with self._lock:
            series = self._series.get(name)
            if series is None:
                return MetricSummary(
                    name=name,
                    count=0,
                    error_count=0,
                    success_count=0,
                    success_rate=0.0,
                    error_rate=0.0,
                    p95_duration_ms=None,
                )
            count = series.count
            error_count = series.error_count
            durations = sorted(series.durations_ms)

        success_count = count - error_count
        rank = max(1, math.ceil(0.95 * len(durations)))
        return MetricSummary(
            name=name,
            count=count,
            error_count=error_count,
            success_count=success_count,
            success_rate=success_count / count,
            error_rate=error_count / count,
            p95_duration_ms=durations[rank - 1],
        )

    def snapshot(self) -> dict[str, MetricSummary]:
        with self._lock:
            names = tuple(self._series)
        return {name: self.summary(name) for name in names}

    def clear(self) -> None:
        with self._lock:
            self._series.clear()
