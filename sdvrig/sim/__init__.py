"""Discrete-event clock. Time is an integer number of microseconds.

Everything in the rig (buses, ECUs, the gateway, the tester) schedules work on one
`Sim`. The tester is the only caller that blocks: it runs the clock forward until
the answer it waits for has arrived or its own timer has expired. That keeps every
scenario deterministic for a given seed and lets a 30-second flash finish in well
under a second of wall time.
"""

from __future__ import annotations

import heapq
import itertools
from typing import Any, Callable


class Timer:
    __slots__ = ("when", "fn", "args", "cancelled")

    def __init__(self, when: int, fn: Callable[..., Any], args: tuple[Any, ...]):
        self.when = when
        self.fn = fn
        self.args = args
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class Sim:
    def __init__(self) -> None:
        self.now = 0
        self._queue: list[tuple[int, int, Timer]] = []
        self._seq = itertools.count()
        self.events_run = 0

    def at(self, when: int, fn: Callable[..., Any], *args: Any) -> Timer:
        if when < self.now:
            raise ValueError(f"cannot schedule in the past: {when} < {self.now}")
        timer = Timer(when, fn, args)
        heapq.heappush(self._queue, (when, next(self._seq), timer))
        return timer

    def later(self, delay_us: int, fn: Callable[..., Any], *args: Any) -> Timer:
        return self.at(self.now + max(0, int(delay_us)), fn, *args)

    def _next_time(self) -> int | None:
        while self._queue and self._queue[0][2].cancelled:
            heapq.heappop(self._queue)
        return self._queue[0][0] if self._queue else None

    def step(self) -> bool:
        if self._next_time() is None:
            return False
        when, _, timer = heapq.heappop(self._queue)
        self.now = when
        self.events_run += 1
        timer.fn(*timer.args)
        return True

    def run_until(self, done: Callable[[], bool], deadline: int) -> bool:
        """Run events until `done()` is true or the clock reaches `deadline`."""
        while not done():
            nxt = self._next_time()
            if nxt is None or nxt > deadline:
                self.now = max(self.now, deadline)
                return done()
            self.step()
        return True

    def run_for(self, duration_us: int) -> None:
        self.run_until(lambda: False, self.now + duration_us)
