# Q2 clipping-arbitration reconstruction — 1 November 2026

Historical source: `8482598be1f2fdd85e7dd1ed553e91a7f34ec5a8`.
Branch: `qhy/development`.
Parent (reconstructed Q1): `81e19f2da27b0380edcd18fcb6b680263438f341`.
Generic tip G remains `8fbf4612f1be85644c867eb51bbf7b7c16b00b46`.

## Scope and equivalence

Only `indi_allsky/exposure/qhy5lii_c.py` and `tests/test_qhy5lii_c.py`
are reconstructed from the historical application/test patch. Both complete
files match historical Q2 byte-for-byte. The application path loses only the
installed snapshot's `app/` prefix. All eight application paths introduced or
modified in Q1, including the precision helper already in G, match Q2 blobs.

The original clipping ceiling, last-safe exposure, hysteretic release rules
and clip_limit telemetry are preserved exactly. Three original regression
methods cover the recorded bright/dark conflict, changed-illumination release,
failed probes, microsecond rounding and SQM/mode/gain state scope. No refactoring,
new policy, generic-controller change or later-stage correction is introduced.

The historical .gitignore deployment-directory addition and host-specific
CLIPPING_CORRECTION.md installer instructions are deliberately not imported.
This note records the portable reconstruction; historical documentation and
all evidence remain preserved in the original snapshot repository.

## Focused checks

From repository root:

```sh
/var/lib/indi-allsky/venv/bin/python3 -B -m unittest tests.test_qhy5lii_c tests.test_adu_precision tests.test_exposure_controller
PYTHONPATH=. /var/lib/indi-allsky/venv/bin/python3 -B tests/check_qhy_integration.py
```

Results: all 30 test methods pass (15 QHY, five precision-helper and 10 generic
controller methods), including 104 existing generic parameterized subtests.
Real ImageWorker construction, QHY selection, synthetic-mask/SQM initialization,
basic-strategy construction without QHY coordination, and the real gain writer
with in-memory acknowledgement/fractional rejection pass. Changed Python source
compiles in memory; whitespace checks pass.

These are reconstruction checks, not a new full processing, VM or camera test.
No worker starts, camera connections, production changes or service operations
occur. Q2 retains the historical limitations subsequently corrected by Q3–Q5,
including strict FITS tolerance and conservative historical-ceiling recovery.
Do not treat this intermediate state as the final qualified controller.

The historical-source commit trailer and external STAGE4_Q2_RESULT_20261101.md
record the old-to-new mapping. Q3 is not started.
