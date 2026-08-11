"""Adaptive pacing for history requests: back off after a FloodWait, ease
back down after a run of clean requests, never leaving the bounds the user
set in Настройки."""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class AdaptiveDelay:
    min_delay: float = 0.2
    max_delay: float = 4.0
    current: float = 1.0
    _streak: int = field(default=0, repr=False)

    def on_flood_wait(self) -> None:
        self.current = min(self.max_delay, self.current * 1.8 + 0.3)
        self._streak = 0

    def on_success(self) -> None:
        self._streak += 1
        if self._streak >= 8:
            self.current = max(self.min_delay, self.current * 0.92)
            self._streak = 0

    def set_bounds(self, min_delay: float, max_delay: float) -> None:
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.current = min(max(self.current, min_delay), max_delay)


@dataclass
class AccountHealth:
    """Rolling request counter + daily FloodWait/pause stats, in-memory only."""
    _request_times: list[float] = field(default_factory=list)
    floodwaits_today: int = 0
    pause_seconds_today: int = 0
    _day: str = field(default_factory=lambda: time.strftime("%Y-%m-%d"))

    def _roll_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self.floodwaits_today = 0
            self.pause_seconds_today = 0

    def note_request(self) -> None:
        self._roll_day()
        now = time.time()
        self._request_times.append(now)
        cutoff = now - 3600
        self._request_times = [t for t in self._request_times if t >= cutoff]

    def note_flood_wait(self, seconds: int) -> None:
        self._roll_day()
        self.floodwaits_today += 1
        self.pause_seconds_today += seconds

    def requests_last_hour(self) -> int:
        self._roll_day()
        cutoff = time.time() - 3600
        return len([t for t in self._request_times if t >= cutoff])
