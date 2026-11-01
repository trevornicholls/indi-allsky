> Historical reconstruction note from `81e19f2da27b0380edcd18fcb6b680263438f341`.
> Claims and IDs below describe the original September-based reconstruction.
> Stage 10B carries this stage onto November upstream; current results are in
> the external `STAGE10B_RESULT_20261101.md`. This is not a new physical qualification.

# Q1 reconstruction checkpoint — 1 November 2026

Historical source: `97176513f54ff5543c471928844ceed909a3d76b`.
New branch: `qhy/development`.
Parent / generic tip G: `8fbf4612f1be85644c867eb51bbf7b7c16b00b46`.

This commit reconstructs Q1 on genuine upstream ancestry. Its historical-source
commit trailer records the mapping; the external Stage 4 Q1 checkpoint record
supplies the resulting full new commit ID after commit creation.

## Application scope

Seven application files reproduce the historical Q1 patch, with `app/` removed:

- `indi_allsky/allsky.py`: shared feedback queue and worker coordination.
- `indi_allsky/camera/indi.py`: gain guard/acknowledgement and native frame context.
- `indi_allsky/capture.py`: opt-in frame gate, identity/startup/mode/SQM integration.
- `indi_allsky/exposure/__init__.py`: strategy registration.
- `indi_allsky/exposure/qhy5lii_c.py`: original Q1 integer feedback policy/gate.
- `indi_allsky/flask/forms.py`: opt-in strategy selection.
- `indi_allsky/image.py`: policy ownership, precision caller, completion and telemetry.

All seven resulting files are byte-identical to historical Q1. The generic
precision helper in processing.py is unchanged from G and also matches Q1.
Original generic correction files/tests remain unchanged. The generic branch
remains at G. Upstream packaging/layout differences are not imported.

## Tests and portability

`tests/test_qhy5lii_c.py` is copied byte-for-byte from Q1: 12 policy/transport
checks for integer limits, exposure priority, settling/stability, brackets,
mode transitions, SQM restoration, feedback order/idle checks, worker replacement,
first-fault retention, native metadata, final property guards and telemetry.

`tests/check_qhy_integration.py` adapts historical check_integration.py:
private saved production configuration is replaced by a deep copy of upstream
base defaults, a synthetic/default mask and a temporary directory. Flask uses
an in-memory SQLite URI selected before application imports. No saved credentials,
production database or external fixture is needed. Real ImageWorker construction,
strategy registration, processor/SQM initialisation and the real INDI gain writer
are exercised; INDI properties/acknowledgements are in memory. A basic-strategy
constructor check confirms it does not require QHY coordination. No worker is
started and no camera/socket connection is made.

Run from repository root using the existing dependency environment:

```sh
/var/lib/indi-allsky/venv/bin/python3 -B -m unittest tests.test_qhy5lii_c tests.test_adu_precision tests.test_exposure_controller
PYTHONPATH=. /var/lib/indi-allsky/venv/bin/python3 -B tests/check_qhy_integration.py
```

Results: 27 test methods pass (including 104 existing generic parameterized
subtests); integration checks pass; syntax compilation and whitespace checks pass.
This verifies reconstruction, not new hardware qualification or a full end-to-end
processing run. No VM build, deployment, service operation or camera test occurs.

## Deliberate intermediate limitations

Q1 is not the final qualified controller. Its known clipping conflict, strict
FITS tolerance and subsequent clipping-recovery/quantisation changes remain for
Q2–Q5. Those later corrections are intentionally not folded into this commit.
Host-specific installer, manifest and deployment instructions from the snapshot
are not imported into this portable application layer. Historical repositories,
qualification material and the running installation remain untouched.
