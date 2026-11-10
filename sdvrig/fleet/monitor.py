"""Fleet monitoring: decide, from vehicle uploads alone, whether a rollout must stop.

Reads only `VehicleDay` records (see sdvrig.fleet.world for what they hold; this
module never imports the world). DTC readouts are decoded from their raw UDS bytes
with the same parser the bench tester uses.

Three ways to decide, sharing thresholds from config/fleet.json ("monitor"):

  stratified    vehicles on the new release against vehicles still on the old one,
                compared only within the same day, region, temperature band and
                hardware revision (a Mantel-Haenszel / CMH score test on event
                counts given exposure). Weather, region mix and hardware mix
                cancel out. On a halt it localises the excess: which hardware
                revisions, and below which temperature.
  pooled        the same new-against-old comparison, all vehicle-days pooled.
  before_after  the whole fleet's DTC rate since the rollout started against the
                week before it, as a fleet dashboard would show it.

A halt needs z >= z_halt, a rate ratio >= min_rate_ratio and at least
min_exposed_events events on the new release.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from sdvrig.uds import TEST_FAILED_THIS_CYCLE, dtc_from_text, dtc_to_text
from sdvrig.vehicle.tester import parse_dtc_records

ECUS = ("ZCU_FRONT", "ZCU_REAR", "BMS", "THERMAL")


@dataclass(frozen=True)
class Halt:
    method: str
    ecu: str
    version: str
    dtc: str
    day: int
    z: float
    rate_ratio: float
    exposed_events: int
    expected_events: float
    localization: dict | None = None


@dataclass
class _Stats:
    a: float = 0.0       # events on the new release
    e: float = 0.0       # events expected on it under no difference
    v: float = 0.0       # variance of a under no difference
    mh_num: float = 0.0
    mh_den: float = 0.0

    def add(self, a: int, b: int, n1: int, n0: int) -> None:
        total = n1 + n0
        m = a + b
        p = n1 / total
        self.a += a
        self.e += m * p
        self.v += m * p * (1 - p)
        self.mh_num += a * n0 / total
        self.mh_den += b * n1 / total

    @property
    def z(self) -> float:
        return (self.a - self.e) / math.sqrt(self.v) if self.v > 0 else 0.0

    @property
    def rate_ratio(self) -> float:
        if self.mh_den == 0:
            return math.inf if self.mh_num > 0 else 1.0
        return self.mh_num / self.mh_den


class _Base:
    method = "base"

    def __init__(self, cfg: dict):
        m = cfg["monitor"]
        self.bands = m["temperature_bands_c"]
        self.z_halt = m["z_halt"]
        self.min_rr = m["min_rate_ratio"]
        self.min_events = m["min_exposed_events"]
        self.loc_min_events = m["localize_min_events"]
        self.loc_z = m["localize_z"]
        # exposure[ecu][version][(day, region, band, hw)] = vehicle-days
        self.exposure: dict = {e: defaultdict(lambda: defaultdict(int)) for e in ECUS}
        # events[ecu][dtc][version][(day, region, band, hw)] = vehicle-days with the DTC failing
        self.events: dict = {e: defaultdict(lambda: defaultdict(lambda: defaultdict(int))) for e in ECUS}
        self.first_seen: dict[tuple[str, str], int] = {}
        self.incumbent: dict[str, str] = {}
        self.halted: set[tuple[str, str]] = set()
        self.halts: list[Halt] = []
        self.records = 0

    def band(self, temp: int) -> int:
        for edge in self.bands:
            if temp <= edge:
                return edge
        return 99

    def observe(self, batch) -> None:
        for rec in batch:
            self.records += 1
            band = self.band(rec.ambient_min_c)
            for i, ecu in enumerate(ECUS):
                version = rec.software[i]
                key = (rec.day, rec.region, band, rec.hardware[i])
                self.exposure[ecu][version][key] += 1
                if (ecu, version) not in self.first_seen:
                    self.first_seen[(ecu, version)] = rec.day
                    self.incumbent.setdefault(ecu, version)
            for ecu, raw in rec.dtc_readouts:
                key = (rec.day, rec.region, band, rec.hardware[ECUS.index(ecu)])
                version = rec.software[ECUS.index(ecu)]
                for code, status in parse_dtc_records(raw):
                    if status & TEST_FAILED_THIS_CYCLE:
                        self.events[ecu][code][version][key] += 1

    def candidates(self):
        for (ecu, version), first in self.first_seen.items():
            if version != self.incumbent.get(ecu) and (ecu, version) not in self.halted:
                yield ecu, version, first

    def decide(self, day: int) -> list[Halt]:
        new = []
        for ecu, version, first in list(self.candidates()):
            best = None
            for code in self.events[ecu]:
                stats = self.evaluate(ecu, version, code, first, day)
                if stats is None:
                    continue
                if stats.z >= self.z_halt and stats.rate_ratio >= self.min_rr and stats.a >= self.min_events:
                    if best is None or stats.z > best[1].z:
                        best = (code, stats)
            if best:
                code, stats = best
                halt = Halt(self.method, ecu, version, dtc_to_text(code), day, round(stats.z, 2),
                            round(stats.rate_ratio, 2) if math.isfinite(stats.rate_ratio) else 999.0,
                            int(stats.a), round(stats.e, 1), self.localize(ecu, version, code, first, day))
                self.halted.add((ecu, version))
                self.halts.append(halt)
                new.append(halt)
        return new

    def evaluate(self, ecu: str, version: str, code: int, first: int, day: int) -> _Stats | None:
        raise NotImplementedError

    def localize(self, ecu, version, code, first, day) -> dict | None:
        return None

    def triage(self, halt: Halt) -> dict | None:
        """Localise again with everything uploaded since the halt: vehicles already on
        the release keep reporting, so the picture sharpens after the rollout stops."""
        first = self.first_seen[(halt.ecu, halt.version)]
        return self.localize(halt.ecu, halt.version, dtc_from_text(halt.dtc), first, None)


class StratifiedMonitor(_Base):
    method = "stratified"

    def _strata(self, ecu, version, code, first, keep=lambda key: True) -> _Stats:
        control = self.incumbent[ecu]
        exp_new = self.exposure[ecu][version]
        exp_old = self.exposure[ecu][control]
        ev_new = self.events[ecu][code][version]
        ev_old = self.events[ecu][code][control]
        stats = _Stats()
        for key, n1 in exp_new.items():
            if key[0] < first or not keep(key):
                continue
            n0 = exp_old.get(key, 0)
            if n0 == 0:
                continue      # no comparable vehicles on the old release in this stratum
            stats.add(ev_new.get(key, 0), ev_old.get(key, 0), n1, n0)
        return stats

    def evaluate(self, ecu, version, code, first, day):
        return self._strata(ecu, version, code, first)

    def _excess(self, s: _Stats) -> bool:
        return s.a >= self.loc_min_events and s.rate_ratio >= self.min_rr and s.z >= self.loc_z

    def _no_excess(self, s: _Stats) -> bool:
        # Needs data: a side with too few events can neither show nor rule out an excess.
        return s.a + s.e >= self.loc_min_events and s.rate_ratio < self.min_rr

    def localize(self, ecu, version, code, first, day) -> dict:
        hws = sorted({key[3] for key in self.exposure[ecu][version]})
        affected = [hw for hw in hws
                    if self._excess(self._strata(ecu, version, code, first, lambda k, h=hw: k[3] == h))]
        hw_keep = (lambda k: k[3] in affected) if affected else (lambda k: True)
        # The boundary is the lowest band edge above which the excess has gone: walk up
        # from the cold end until the warmer side shows no excess. A defect that shows
        # at every temperature never finds one.
        below = None
        for edge in sorted(self.bands):
            cold = self._strata(ecu, version, code, first, lambda k, t=edge: hw_keep(k) and k[2] <= t)
            warm = self._strata(ecu, version, code, first, lambda k, t=edge: hw_keep(k) and k[2] > t)
            if self._excess(cold) and self._no_excess(warm):
                below = edge
                break
        return {"hardware": affected if affected and len(affected) < len(hws) else "all",
                "ambient_at_or_below_c": below}


class PooledMonitor(_Base):
    method = "pooled"

    def evaluate(self, ecu, version, code, first, day):
        control = self.incumbent[ecu]
        n1 = sum(n for k, n in self.exposure[ecu][version].items() if k[0] >= first)
        n0 = sum(n for k, n in self.exposure[ecu][control].items() if k[0] >= first)
        if not n1 or not n0:
            return None
        a = sum(n for k, n in self.events[ecu][code][version].items() if k[0] >= first)
        b = sum(n for k, n in self.events[ecu][code][control].items() if k[0] >= first)
        stats = _Stats()
        stats.add(a, b, n1, n0)
        return stats


class BeforeAfterMonitor(_Base):
    method = "before_after"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.window_days = cfg["monitor"]["before_after_window_days"]

    def evaluate(self, ecu, version, code, first, day):
        def totals(lo, hi):
            exp = sum(n for ver in self.exposure[ecu].values() for k, n in ver.items() if lo <= k[0] <= hi)
            ev = sum(n for ver in self.events[ecu][code].values() for k, n in ver.items() if lo <= k[0] <= hi)
            return exp, ev
        exp_before, ev_before = totals(first - self.window_days, first - 1)
        exp_after, ev_after = totals(first, day)
        if exp_before == 0 or exp_after == 0 or ev_before == 0:
            return None
        rate_before = ev_before / exp_before
        expected = rate_before * exp_after
        stats = _Stats()
        stats.a, stats.e, stats.v = ev_after, expected, expected
        stats.mh_num, stats.mh_den = ev_after / exp_after, rate_before
        return stats


MONITORS = {"stratified": StratifiedMonitor, "pooled": PooledMonitor, "before_after": BeforeAfterMonitor}


@dataclass
class NoMonitor:
    """Lets a rollout run to 100% whatever happens; the reference for exposure."""

    method: str = "none"
    halts: list = field(default_factory=list)

    def observe(self, batch) -> None:
        pass

    def decide(self, day: int) -> list:
        return []
