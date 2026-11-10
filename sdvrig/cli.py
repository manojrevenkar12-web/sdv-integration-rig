"""Command line: python -m sdvrig <command>.

  scenarios     write the dev and held-out scenario sets and lock the held-out ones
  flash-eval    run flashing scenarios with both flashers      (--set dev|heldout)
  fleet-eval    run fleet scenarios with every monitor, triage (--set dev|heldout)
  crosscheck    flash each ECU and check the captures with scapy and the Rust decoder
  bench         timing numbers for the dashboard
  serve         serve the simulated vehicle on a real DoIP port
  report        regenerate reports/REPORT.md and the README dashboard from reports/*.json

Held-out sets run once. The first run is written to reports/heldout/ and a second
run is refused unless --rerun is given, in which case it goes to a separate file
and the first run stays the reported one.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from sdvrig.vehicle import ROOT

SCEN = ROOT / "scenarios"
REPORTS = ROOT / "reports"
LOCK = ROOT / "config" / "heldout.lock"
SETS = {
    "flash": {"dev": ("flash_dev.json", 1, 10), "heldout": ("flash_heldout.json", 2026, 12)},
    "fleet": {"dev": ("fleet_dev.json", 11, 2), "heldout": ("fleet_heldout.json", 2027, 5)},
}


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=False) + "\n")


def cmd_scenarios(args) -> int:
    from sdvrig import scenarios as flash_sc
    from sdvrig.fleet import evaluate as fleet_ev

    SCEN.mkdir(exist_ok=True)
    lock = json.loads(LOCK.read_text()) if LOCK.exists() else {}
    for kind, sets in SETS.items():
        gen = flash_sc.generate if kind == "flash" else fleet_ev.generate
        for name, (fname, seed, per) in sets.items():
            path = SCEN / fname
            if name == "heldout" and fname in lock and not args.force:
                print(f"{fname}: locked ({lock[fname][:12]}), not regenerated")
                continue
            digest = flash_sc.write_set(path, gen(seed, per, f"{kind}-{name}"))
            if name == "heldout":
                lock[fname] = digest
            print(f"{fname}: {len(json.loads(path.read_text()))} scenarios, sha256 {digest[:12]}")
    _write_json(LOCK, lock)
    return 0


def _load_set(kind: str, name: str) -> list[dict]:
    from sdvrig.scenarios import sha256_file

    fname = SETS[kind][name][0]
    path = SCEN / fname
    if name == "heldout":
        lock = json.loads(LOCK.read_text())
        if sha256_file(path) != lock.get(fname):
            raise SystemExit(f"{fname} does not match config/heldout.lock; held-out scenarios were changed")
    return json.loads(path.read_text())


def _out_path(kind: str, name: str, rerun: bool) -> Path:
    if name == "dev":
        return REPORTS / f"{kind}_dev.json"
    first = REPORTS / "heldout" / f"{kind}_first_run.json"
    if first.exists() and not rerun:
        raise SystemExit(f"{first.relative_to(ROOT)} exists: held-out scenarios run once. "
                         "Use --rerun to write a separate rerun file.")
    return REPORTS / "heldout" / (f"{kind}_rerun.json" if first.exists() else f"{kind}_first_run.json")


def summarize_flash(records: list[dict]) -> dict:
    out = {}
    for flasher in sorted({r["flasher"] for r in records}):
        rs = [r for r in records if r["flasher"] == flasher]
        fam = defaultdict(lambda: [0, 0])
        failed_checks = Counter()
        for r in rs:
            fam[r["family"]][1] += 1
            fam[r["family"]][0] += r["passed"]
            for k, ok in r["checks"].items():
                failed_checks[k] += not ok
        updated = [r["result"]["duration_ms"] for r in rs if r["result"]["outcome"] == "updated"]
        out[flasher] = {
            "scenarios": len(rs),
            "passed": sum(r["passed"] for r in rs),
            "checks_failed": dict(failed_checks),
            "bricked": failed_checks["no_brick"],
            "families": {k: {"passed": v[0], "of": v[1]} for k, v in fam.items()},
            "median_update_ms": statistics.median(updated) if updated else None,
            "block_retries": sum(r["result"]["block_retries"] for r in rs),
            "restarts": sum(max(0, r["result"]["attempts"] - 1) for r in rs),
        }
    return out


def cmd_flash_eval(args) -> int:
    from sdvrig.scenarios import run_scenario

    scenarios = _load_set("flash", args.set)
    out_path = _out_path("flash", args.set, args.rerun)
    t0 = time.perf_counter()
    records = []
    for s in scenarios:
        for flasher in ("robust", "baseline"):
            records.append(run_scenario(s, flasher).to_dict())
    data = {"set": args.set, "scenarios": len(scenarios), "seconds": round(time.perf_counter() - t0, 1),
            "summary": summarize_flash(records), "records": records}
    _write_json(out_path, data)
    for flasher, s in data["summary"].items():
        print(f"{flasher:9s} {s['passed']}/{s['scenarios']} passed, checks failed {s['checks_failed']}")
    print(f"wrote {out_path.relative_to(ROOT)} in {data['seconds']} s")
    return 0


def cmd_fleet_eval(args) -> int:
    from sdvrig.fleet.evaluate import run_fleet_scenario, score
    from sdvrig.fleet.triage import triage
    from sdvrig.fleet.world import load_fleet_config

    cfg = load_fleet_config()
    scenarios = _load_set("fleet", args.set)
    out_path = _out_path("fleet", args.set, args.rerun)
    t0 = time.perf_counter()
    results, tickets = [], []
    for s in scenarios:
        res = run_fleet_scenario(s, cfg, vehicles=args.vehicles)
        results.append(res)
        for rel in res["methods"]["stratified"]["releases"]:
            if rel["halt"]:
                release = next(r for r in s["releases"] if (r["ecu"], r["version"]) == (rel["ecu"], rel["version"]))
                t = triage(rel["halt"], release, cfg["owners"])
                t.update({"scenario": s["id"], "defective": rel["defective"]})
                tickets.append(t)
        print(f"{s['id']}: done ({time.perf_counter() - t0:.0f} s)", flush=True)
    summary = score(results)
    repro = [t for t in tickets if t["defective"] and t.get("reproduced") is not None]
    data = {"set": args.set, "scenarios": len(scenarios), "seconds": round(time.perf_counter() - t0, 1),
            "summary": summary,
            "triage": {"tickets": len(tickets), "bench_repro_attempted": len(repro),
                       "bench_repro_confirmed": sum(1 for t in repro if t["reproduced"]),
                       "soak_test_needed": sum(1 for t in tickets if t.get("reproduced") is None)},
            "tickets": tickets, "results": results}
    _write_json(out_path, data)
    for method, s in summary.items():
        print(f"{method:13s} caught {s['caught']}/{s['defective_releases']}, false halts "
              f"{s['false_halts']}/{s['healthy_releases']}, median days {s['median_days_to_halt']}")
    print(f"wrote {out_path.relative_to(ROOT)} in {data['seconds']} s")
    return 0


def cmd_crosscheck(args) -> int:
    from sdvrig.capture.crosscheck import crosscheck
    from sdvrig.flash.campaign import flasher_for
    from sdvrig.flash.image import build_image
    from sdvrig.vehicle import build_vehicle

    out = []
    cap_dir = REPORTS / "captures"
    for ecu in ("ZCU_FRONT", "ZCU_REAR", "BMS", "THERMAL"):
        v = build_vehicle()
        size = v.node(ecu).ecu.spec.image_bytes
        flasher_for(v, "robust", ecu).flash(build_image(ecu, "4.0.0", size))
        keep = ecu in ("ZCU_FRONT", "BMS")
        result = crosscheck(v, ecu, cap_dir if keep else Path("/tmp/sdvrig-captures"), f"flash-{ecu}")
        if not keep:
            result["files"] = "not kept"
        out.append(result)
        print(ecu, [(c["decoder"], c.get("agrees", c.get("skipped"))) for c in result["checks"]])
    _write_json(REPORTS / "crosscheck.json", out)
    return 0


def cmd_bench(args) -> int:
    from sdvrig.fleet.evaluate import run_fleet_scenario
    from sdvrig.fleet.world import load_fleet_config
    from sdvrig.flash.campaign import flasher_for
    from sdvrig.flash.image import build_image
    from sdvrig.vehicle import build_vehicle

    flashes = {}
    for ecu in ("ZCU_FRONT", "ZCU_REAR", "BMS", "THERMAL"):
        walls, sims = [], []
        for _ in range(5):
            t0 = time.perf_counter()
            v = build_vehicle()
            node = v.node(ecu)
            r = flasher_for(v, "robust", ecu).flash(build_image(ecu, "4.0.0", node.ecu.spec.image_bytes))
            walls.append((time.perf_counter() - t0) * 1000)
            sims.append(r.duration_ms)
        bus = v.buses[node.bus.name]
        flashes[ecu] = {"image_kib": node.ecu.spec.image_kib, "bus": node.bus.name, "fd": node.bus.fd,
                        "simulated_ms": statistics.median(sims), "wall_ms": round(statistics.median(walls), 1),
                        "throughput_kib_s": round(node.ecu.spec.image_kib / (statistics.median(sims) / 1000), 1),
                        "frames": bus.frames}
    cfg = load_fleet_config()
    scen = json.loads((SCEN / "fleet_dev.json").read_text())[0]
    t0 = time.perf_counter()
    res = run_fleet_scenario(scen, cfg, methods=("stratified",))
    wall = time.perf_counter() - t0
    records = res["methods"]["stratified"]["records"]
    data = {"flash": flashes, "fleet": {"vehicle_days": records, "seconds": round(wall, 2),
                                        "vehicle_days_per_s": int(records / wall)},
            "python": sys.version.split()[0]}
    _write_json(REPORTS / "bench.json", data)
    print(json.dumps(data, indent=1))
    return 0


def cmd_showcase(args) -> int:
    """Two dev scenarios run by both flashers, with per-step times and bus activity, for the dashboard."""
    from sdvrig.scenarios import run_scenario_with_vehicle

    dev = {s["id"]: s for s in json.loads((SCEN / "flash_dev.json").read_text())}
    picks = [sid for sid in args.ids]
    out = []
    for sid in picks:
        s = dev[sid]
        runs = {}
        for flasher in ("robust", "baseline"):
            rec, v = run_scenario_with_vehicle(s, flasher)
            bus = v.buses[{"ZCU_FRONT": "zone-front", "ZCU_REAR": "zone-rear"}.get(s["ecu"], "powertrain")]
            end_ms = rec.result["duration_ms"] + 500
            bins = [0] * (int(end_ms // 50) + 1)
            for r in bus.trace:
                i = int(r.time_us / 1000 // 50)
                if i < len(bins):
                    bins[i] += 1
            runs[flasher] = {"record": rec.to_dict(), "frames_per_50ms": bins, "faults": rec.details.get("faults_fired")}
        out.append({"scenario": s, "runs": runs})
    _write_json(REPORTS / "showcase.json", out)
    print("wrote reports/showcase.json")
    return 0


def cmd_serve(args) -> int:
    from sdvrig.doip.server import DoIPServer
    from sdvrig.vehicle import build_vehicle

    v = build_vehicle()
    server = DoIPServer(v, host=args.host, port=args.port, speed=args.speed)
    print(f"DoIP gateway {v.vcfg['vin']} on {args.host}:{server.port} (TCP and UDP), speed x{args.speed}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


def cmd_report(args) -> int:
    from sdvrig.report import build_report

    build_report()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sdvrig")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scenarios")
    s.add_argument("--force", action="store_true", help="regenerate even locked held-out sets")
    for name in ("flash-eval", "fleet-eval"):
        s = sub.add_parser(name)
        s.add_argument("--set", choices=("dev", "heldout"), default="dev")
        s.add_argument("--rerun", action="store_true")
        if name == "fleet-eval":
            s.add_argument("--vehicles", type=int, default=None)
    sub.add_parser("crosscheck")
    sub.add_parser("bench")
    s = sub.add_parser("showcase")
    s.add_argument("ids", nargs="*", default=["flash-dev-power_cut_transfer-01", "flash-dev-bad_build-01"])
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=13400)
    s.add_argument("--speed", type=float, default=1.0)
    sub.add_parser("report")
    args = p.parse_args(argv)
    return {"scenarios": cmd_scenarios, "flash-eval": cmd_flash_eval, "fleet-eval": cmd_fleet_eval,
            "crosscheck": cmd_crosscheck, "bench": cmd_bench, "showcase": cmd_showcase, "serve": cmd_serve,
            "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
