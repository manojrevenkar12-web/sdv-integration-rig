"""Generate reports/REPORT.md, the README dashboard block and web/public/data.json
from the JSON results in reports/. Numbers are never typed by hand: rerun
`python -m sdvrig report` after any evaluation.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from sdvrig.vehicle import ROOT

REPORTS = ROOT / "reports"
START, END = "<!-- dashboard:start -->", "<!-- dashboard:end -->"


def _load(name: str):
    path = REPORTS / name
    return json.loads(path.read_text()) if path.exists() else None


def _pct(x) -> str:
    return "n/a" if x is None else f"{round(100 * x)}%"


def _tests() -> dict | None:
    path = REPORTS / "tests.xml"
    if not path.exists():
        return None
    root = ET.parse(path).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    total = int(suite.get("tests", 0))
    bad = int(suite.get("failures", 0)) + int(suite.get("errors", 0))
    skipped = int(suite.get("skipped", 0))
    return {"total": total, "passed": total - bad - skipped, "failed": bad, "skipped": skipped,
            "seconds": round(float(suite.get("time", 0)), 1)}


def collect() -> dict:
    return {
        "flash_heldout": _load("heldout/flash_first_run.json"),
        "flash_dev": _load("flash_dev.json"),
        "fleet_heldout": _load("heldout/fleet_first_run.json"),
        "fleet_dev": _load("fleet_dev.json"),
        "crosscheck": _load("crosscheck.json"),
        "bench": _load("bench.json"),
        "showcase": _load("showcase.json"),
        "tests": _tests(),
    }


def dashboard_md(d: dict) -> str:
    lines = []
    fh, flh, xc, bench, tests = d["flash_heldout"], d["fleet_heldout"], d["crosscheck"], d["bench"], d["tests"]
    if fh:
        r, b = fh["summary"]["robust"], fh["summary"]["baseline"]
        n = fh["scenarios"]
        lines += [
            f"**Flashing under faults** — {n} held-out scenarios in {len(r['families'])} fault families, run once "
            f"(locked by SHA-256 before the first run):",
            "",
            "| Check, on the vehicle's own state | Robust flasher | Happy-path baseline |",
            "|---|---:|---:|",
            f"| Scenarios passing every check | **{r['passed']} / {n}** | {b['passed']} / {n} |",
            f"| Reported the wrong outcome | {r['checks_failed'].get('outcome', 0)} | {b['checks_failed'].get('outcome', 0)} |",
            f"| Report disagreed with what the ECU runs | {r['checks_failed'].get('truthful_report', 0)} | "
            f"{b['checks_failed'].get('truthful_report', 0)} |",
            f"| Left a fault code the scenario did not cause | {r['checks_failed'].get('no_stray_dtcs', 0)} | "
            f"{b['checks_failed'].get('no_stray_dtcs', 0)} |",
            f"| Left an ECU outside the default session or muted | {r['checks_failed'].get('vehicle_restored', 0)} | "
            f"{b['checks_failed'].get('vehicle_restored', 0)} |",
            f"| Bricked ECUs | {r['bricked']} | {b['bricked']} |",
            f"| Blocks repeated / programming restarts | {r['block_retries']} / {r['restarts']} | "
            f"{b['block_retries']} / {b['restarts']} |",
            "",
        ]
    if flh:
        s = flh["summary"]
        st, po, ba, no = s["stratified"], s["pooled"], s["before_after"], s["none"]
        tr = flh["triage"]
        lines += [
            f"**Fleet monitoring** — {flh['scenarios']} held-out fleets of 8,000 vehicles over 45 winter days, "
            f"{st['defective_releases']} bad releases and {st['healthy_releases']} healthy ones, staged rollouts "
            "that halt when the monitor says so:",
            "",
            "| Monitor | Bad releases halted | Healthy releases halted | Median days to halt | "
            "Median fleet share on the bad release | Bad-release vehicle-days avoided |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for name, label, m in (("stratified", "**Stratified (this repo)**", st), ("pooled", "Pooled new vs old", po),
                               ("before_after", "Fleet before/after", ba), ("none", "No monitor", no)):
            days = "–" if m["median_days_to_halt"] is None else f"{m['median_days_to_halt']:g}"
            lines.append(f"| {label} | {m['caught']} / {m['defective_releases']} | {m['false_halts']} / "
                         f"{m['healthy_releases']} | {days} | {_pct(m['median_fleet_share_on_bad_release'])} | "
                         f"{_pct(m['median_bad_exposure_avoided'])} |")
        lines += [
            "",
            f"Triage after a halt: the localisation (hardware revision and temperature) was right for "
            f"{st['localization_correct']} of {st['localization_checked']} halts; "
            f"{tr['bench_repro_confirmed']} of {tr['bench_repro_attempted']} bench reproductions in the simulated "
            f"climate chamber confirmed the fleet finding; {tr['soak_test_needed']} tickets asked for a soak test "
            "because no single condition isolated the defect.",
            "",
        ]
    if xc:
        msgs = sum(x["messages"] for x in xc)
        agree = {c["decoder"]: 0 for c in xc[0]["checks"]}
        for x in xc:
            for c in x["checks"]:
                agree[c["decoder"]] += c.get("equal", 0) if c.get("agrees") else 0
        frames = sum(x["can_frames"] for x in xc)
        lines += [
            f"**Independent decoders** — {msgs} diagnostic messages from four flashes ({frames:,} CAN and CAN FD "
            "frames) decoded from the pcap captures by code that shares nothing with the rig: "
            + ", ".join(f"{k} {v} / {msgs}" for k, v in agree.items()) + ".",
            "",
        ]
    if bench:
        lines += ["| ECU | Bus | Image | Flash, simulated | Effective rate | Wall time |", "|---|---|---:|---:|---:|---:|"]
        for ecu, b in bench["flash"].items():
            lines.append(f"| {ecu} | {b['bus']} ({'CAN FD 2 Mbit/s' if b['fd'] else 'CAN 500 kbit/s'}) | "
                         f"{b['image_kib']} KiB | {b['simulated_ms'] / 1000:.2f} s | {b['throughput_kib_s']} KiB/s | "
                         f"{b['wall_ms']:.0f} ms |")
        f = bench["fleet"]
        lines += ["", f"Fleet simulation and monitoring: {f['vehicle_days_per_s']:,} vehicle-days per second "
                      f"(Python {bench['python']})."]
        if tests:
            lines[-1] += f" Tests: {tests['passed']} passed, {tests['failed']} failed, {tests['skipped']} skipped."
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def report_md(d: dict) -> str:
    out = ["# sdv-integration-rig report", "", "Generated by `python -m sdvrig report` from `reports/*.json`.", "",
           "## Dashboard", "", dashboard_md(d)]
    fh = d["flash_heldout"]
    if fh:
        out += ["## Held-out flashing, by family", "", "| Family | Robust | Baseline |", "|---|---:|---:|"]
        r, b = fh["summary"]["robust"]["families"], fh["summary"]["baseline"]["families"]
        for fam in r:
            out.append(f"| {fam} | {r[fam]['passed']}/{r[fam]['of']} | {b[fam]['passed']}/{b[fam]['of']} |")
        out.append("")
    fd = d["flash_dev"]
    if fd:
        r, b = fd["summary"]["robust"], fd["summary"]["baseline"]
        out += [f"Dev set (used while building): robust {r['passed']}/{r['scenarios']}, baseline "
                f"{b['passed']}/{b['scenarios']}.", ""]
    flh = d["fleet_heldout"]
    if flh:
        out += ["## Held-out fleet: missed bad releases", ""]
        for res in flh["results"]:
            for rel in res["methods"]["stratified"]["releases"]:
                if rel["defective"] and not rel["halt"]:
                    df = rel["defect"]
                    out.append(f"- {res['id']}: {rel['ecu']} {rel['version']}, {df['dtc']} "
                               f"{'below ' + str(df['below_c']) + ' °C' if df['when'] == 'cold' else 'at all temperatures'}"
                               f"{' on ' + df['hw'] if df.get('hw') else ''}, {df['rate']} per affected vehicle-day")
        out += ["", "## Held-out fleet: tickets with a bench reproduction", ""]
        for t in flh["tickets"]:
            if t.get("reproduced") is not None:
                out.append(f"- {t['scenario']}: {t['title']} → reproduced: {t['reproduced']} (owner: {t['owner']})")
        out.append("")
    fdv = d["fleet_dev"]
    if fdv:
        s = fdv["summary"]["stratified"]
        out += [f"Fleet dev set: stratified caught {s['caught']}/{s['defective_releases']}, false halts "
                f"{s['false_halts']}/{s['healthy_releases']}.", ""]
    return "\n".join(out)


def web_data(d: dict) -> dict:
    out = {"flash": None, "fleet": None, "crosscheck": d["crosscheck"], "bench": d["bench"], "tests": d["tests"],
           "showcase": d["showcase"]}
    fh = d["flash_heldout"]
    if fh:
        out["flash"] = {"scenarios": fh["scenarios"], "summary": fh["summary"]}
    flh = d["fleet_heldout"]
    if flh:
        out["fleet"] = {"scenarios": flh["scenarios"], "summary": flh["summary"], "triage": flh["triage"],
                        "tickets": [t for t in flh["tickets"] if t.get("reproduced") is not None][:6]}
    return out


def build_report() -> None:
    d = collect()
    (REPORTS / "REPORT.md").write_text(report_md(d))
    readme = ROOT / "README.md"
    if readme.exists():
        text = readme.read_text()
        block = f"{START}\n{dashboard_md(d)}{END}"
        text = re.sub(re.escape(START) + r".*?" + re.escape(END), lambda m: block, text, flags=re.S)
        readme.write_text(text)
    web = ROOT / "web" / "public"
    if web.exists():
        (web / "data.json").write_text(json.dumps(web_data(d), indent=1) + "\n")
    print("wrote reports/REPORT.md" + (" and the README dashboard" if readme.exists() else ""))
