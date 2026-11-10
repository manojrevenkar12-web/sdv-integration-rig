"""From a fleet halt to a bench reproduction and a ticket for the owning team.

The monitor says: release X of ECU Y raises DTC Z, on hardware H, at or below T.
Triage takes the release artifact (the image the rollout installed), builds the
bench vehicle with that hardware revision, flashes the release with the robust
flasher and reads the fault memory over DoIP:

  1. at room temperature           (what the bench saw before release)
  2. at the localised temperature  (the field condition)
  3. controls: the other hardware revisions at that temperature, and the previous
     release at that temperature on the affected hardware

A reproduction is confirmed when the DTC appears in 2 and in none of 1 or 3.
Defects that show at every temperature as a rate increase have no deterministic
bench condition; triage says so and asks for a soak test instead of guessing.
"""

from __future__ import annotations

from sdvrig import uds
from sdvrig.flash.campaign import flasher_for
from sdvrig.flash.image import build_image
from sdvrig.uds import dtc_from_text, dtc_to_text
from sdvrig.vehicle import build_vehicle, load_config

HW_PREFIX = {"ZCU_FRONT": "SIM-ZCUF-", "ZCU_REAR": "SIM-ZCUR-", "BMS": "SIM-BMS-", "THERMAL": "SIM-THM-"}


def release_artifact(release: dict, size_bytes: int) -> bytes:
    """The image the release pipeline built for this release (the artifact registry)."""
    defect = release.get("defect") or {}
    cold = None
    if defect.get("when") == "cold":
        cold = defect["dtc"] + (f"@{defect['hw']}" if defect.get("hw") else "")
    return build_image(release["ecu"], release["version"], size_bytes, cold_defect=cold)


def _bench_run(ecu: str, hw: str | None, image: bytes | None, ambient_c: int) -> dict:
    overrides = {ecu: {"hardware": HW_PREFIX[ecu] + hw}} if hw else None
    v = build_vehicle(overrides=overrides)
    flashed = None
    if image is not None:
        flashed = flasher_for(v, "robust", ecu).flash(image)
    v.tester.clear_dtcs(v.addr(ecu))
    v.node(ecu).ecu.set_ambient(ambient_c)      # climate chamber
    v.settle(500)
    dtcs = [dtc_to_text(code) for code, status in v.tester.read_dtcs(v.addr(ecu), uds.TEST_FAILED)]
    version = v.tester.read_did(v.addr(ecu), uds.DID_SOFTWARE_VERSION).decode()
    return {"hardware": hw or "default", "ambient_c": ambient_c, "version": version,
            "flash": flashed.outcome if flashed else "not flashed", "dtcs": dtcs}


def triage(halt: dict, release: dict, owners: dict[str, str]) -> dict:
    ecu, dtc = halt["ecu"], halt["dtc"]
    loc = halt.get("triage_localization") or halt.get("localization") or {}
    vcfg, _ = load_config()
    spec = next(e for e in vcfg["ecus"] if e["name"] == ecu)
    size = spec["image_kib"] * 1024
    all_hw = sorted({h for h in _known_hw(ecu)})
    hws = loc.get("hardware")
    affected = all_hw if hws in (None, "all") else list(hws)
    below = loc.get("ambient_at_or_below_c")
    ticket = {
        "owner": owners.get(ecu, "unassigned"),
        "title": f"{ecu} {halt['version']} raises {dtc}"
                 + (f" on {', '.join(affected)}" if hws not in (None, 'all') else "")
                 + (f" at or below {below} °C" if below is not None else ""),
        "evidence": {k: halt[k] for k in ("z", "rate_ratio", "exposed_events", "expected_events", "day")},
        "localization": loc,
        "runs": [],
    }
    if below is None:
        ticket["reproduced"] = None
        ticket["next_step"] = ("no temperature or hardware condition isolates the excess: run a soak test "
                               "of the release on the bench fleet instead of a single repro")
        return ticket
    image = release_artifact(release, size)
    cold = below - 5
    runs = [("room", affected[0], image, 20, True), ("field", affected[0], image, cold, False)]
    runs += [("control-hw", hw, image, cold, True) for hw in all_hw if hw not in affected]
    runs += [("control-previous", affected[0], None, cold, True)]   # the release it replaced
    reproduced = True
    for label, hw, img, temp, expect_clean in runs:
        res = _bench_run(ecu, hw, img, temp)
        res["role"] = label
        has = dtc in res["dtcs"]
        res["as_expected"] = (not has) if expect_clean else has
        reproduced &= res["as_expected"]
        ticket["runs"].append(res)
    ticket["reproduced"] = reproduced
    ticket["next_step"] = ("bench repro confirmed: keep the rollout halted, attach the repro to the fix, "
                           "and add the field condition to the release gate" if reproduced else
                           "fleet signal not reproduced on the bench: check the localisation and the hardware mapping")
    return ticket


def _known_hw(ecu: str) -> list[str]:
    from sdvrig.fleet.world import load_fleet_config
    return list(load_fleet_config()["world"]["hardware"][ecu])


__all__ = ["triage", "release_artifact", "dtc_from_text"]
