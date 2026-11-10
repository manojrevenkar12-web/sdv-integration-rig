# AGENTS.md

## Fast feedback

```bash
python -m pytest -m "not slow"          # about 10 s
(cd tools/isotp-trace && cargo test --release)
(cd web && npx tsc --noEmit && npx vitest run)
python -m sdvrig report                 # after any evaluation
```

## Invariants

1. **Flashers reach the vehicle only through the tester.** `sdvrig/flash/campaign.py` may use the
   tester, addresses and the functional address; it never touches nodes, ECUs, buses or power.
   `tests/test_flash.py` checks this on the source.
2. **The harness checks the vehicle, not the report.** The five checks in `sdvrig/scenarios` read ECU
   state (running slot, trial, DTCs, session, communication). A flasher's own result is only compared
   with that.
3. **The running slot is never written.** Erase and download target the inactive slot. Erase is refused
   while a trial boot is uncommitted.
4. **Truth stays in the world.** `sdvrig/fleet/monitor.py` never imports the world, the evaluation or
   the scenarios. `VehicleDay` holds only what a vehicle uploads. DTCs travel as raw `59 02` bytes.
5. **Settings live in config.** Timings, thresholds and policies are in `config/*.json`. No tuning
   constants go in code.
6. **Reports are generated.** README numbers between the dashboard markers, `reports/REPORT.md` and
   `web/public/data.json` come from `python -m sdvrig report`. Never edit them by hand.

## Held-out rule

`scenarios/flash_heldout.json` and `scenarios/fleet_heldout.json` are locked in `config/heldout.lock`.
They run once, after the flasher and monitor are frozen. The first run in `reports/heldout/` is the
reported one. Do not tune on held-out results. Do not regenerate the held-out sets. A rerun goes to
`*_rerun.json` and never replaces the first run.
