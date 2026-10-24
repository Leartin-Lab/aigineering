# Offline direct Harness protocol fixture

This package runs a scripted native tool-call conversation directly against the
frozen pure calibration tools and Fraction oracle. It bypasses Aigineering task
scheduling and commitment, but it does not call a provider or demonstrate
model-driven planning. The scripted calls are a protocol fixture only.

Run from a source checkout:

    python examples/thermal_calibration/direct-run.py \
      --case short-interval --scenario transient --output-root /tmp/calibration-runs

Each unique run ID gets a new directory containing JSONL events, a manifest,
and separate stdout/stderr captures. Existing run IDs are refused. The transient
scenario injects one classified tool failure and records the error returned to
the scripted client before it retries. No credentials or provider settings are
read. Usage and total cost are unknown, not zero.

HarnessCandidateAdapter is intentionally not used: that adapter signs
Candidates and routes actions through the Runtime Worker compiler. This fixture
only exercises one in-process native tool-call loop. A real provider client and
a fair Runtime-versus-direct evaluation require a separate G plan and valid
authorization. Public eval fixtures are not confidential held-out data. The
project wheel does not include this examples package; validate the examples
bundle from the source distribution separately from the installed wheel.
