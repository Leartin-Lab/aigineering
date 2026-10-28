# Run the calibration example from the CLI

This preset runs a fixed synthetic case through ordinary planning, two scoped
tool observations, a report, and independent acceptance. The deterministic
LocalWorker in `workers.py` is an explicitly configured verifier. Planner and
researcher use the configured DeepSeek provider; a local verifier is not a
second model or an independent source of factual knowledge.

From the repository checkout, set `AIGINEERING_API_KEY` and a reachable Redis
URL, then run:

```bash
PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}" \
  AIGINEERING_REDIS_URL=redis://127.0.0.1:6379/0 \
  aig -C /path/to/workspace run --config "$PWD/examples/thermal_calibration/run.toml"
```

The run publishes the training case and standard as exact Assets. The planner
chooses ordinary runtime work; it does not receive a prebuilt business task
graph. Research calls `normalize_calibration` and `analyze_calibration` through
ToolWorker, then publishes the report and its source/evidence links. The local
verifier rechecks the report with the separate Fraction oracle and submits a
signed attestation for the exact root output and frozen input Assets.
The verifier checks copied observation contents and their ordered IDs as
business evidence. Those copied IDs do not prove signed tool history; the
integration tests separately inspect the Store's committed tool publications.

To export a qualified report, use the root Contract ID printed by the run:

```bash
aig -C /path/to/workspace task result ROOT_CONTRACT_ID \
  --output calibration_report --to calibration-report.json
```

This package does not provide a direct Harness baseline, provider cost study,
or real-world calibration assessment. The checked-in eval fixtures are public
and are not confidential test data.
