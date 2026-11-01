> Historical reconstruction note from `d4f20f26eb5b9985afcb1340f7b116b09729979c`.
> Claims and IDs below describe the original September-based reconstruction.
> Stage 10B carries this stage onto November upstream; current results are in
> the external `STAGE10B_RESULT_20261101.md`. This is not a new physical qualification.

# Q4 bounded clipping-ceiling recovery — 1 November 2026

Historical source: `f445f2c08ef4547ada9c2dc196a38fe20a5da217`.
Branch: `qhy/development`.
Parent Q3: `9cc6a619c506ec801562da57ed6ecd415f58758d`.
Generic G: `8fbf4612f1be85644c867eb51bbf7b7c16b00b46` (unchanged).

## Reconstruction scope

Only `indi_allsky/exposure/qhy5lii_c.py` changes in application source. Its
complete blob matches historical Q4 exactly after removing the installed-tree
`app/` prefix. All eight Q1–Q4 application blobs match the historical Q4 state.
Generic fixes/helper, frame identity checks and unrelated source stay unchanged.

Historical behaviour is preserved: qualified safe exposures and an earlier
fallback bound protective reductions; two safe samples qualify bounded recovery
probes; safe/bad brackets close without perpetual halving/ramping; changed
conditions permit reconsideration. Telemetry includes clip_bad and clip_safe.
No new policy, refactoring, configuration or quantisation logic is added.

The original test patch is applied unchanged. The sole difference from the
historical complete test file remains the accepted Q3 selected-header fixture
adaptation. No new fixture or test framework is required. Host-specific replay
scripts, installation state and development evidence are not imported into the
shared repository; their historical originals remain preserved.

## Focused verification

From repository root:

```sh
/var/lib/indi-allsky/venv/bin/python3 -B -m unittest tests.test_qhy5lii_c tests.test_adu_precision tests.test_exposure_controller
PYTHONPATH=. /var/lib/indi-allsky/venv/bin/python3 -B tests/check_qhy_integration.py
```

All 36 test methods pass (21 QHY, five precision and 10 generic), including
110 subtests (104 generic and six FITS-rejection cases). The four original
closed-loop recovery tests cover the recorded-flash response model, static
clipping-boundary closure, previous-safe fallback after a small clipping spike,
and dimmer/brighter sky changes. The original Q4 changes to earlier clipping
regressions are retained exactly. These response functions are synthetic models,
not new camera measurements or a rerun of the complete historical overnight replay.

Real worker/QHY/basic construction, synthetic-mask/SQM setup and the real INDI
gain writer with in-memory acknowledgement/fraction rejection pass. Python
syntax compilation and whitespace checks pass. Committed application blobs
are checked after commit creation; the external STAGE4_Q4_RESULT_20261101.md
records the full historical-to-reconstructed mapping.

No camera connection, worker start, service operation, VM build, production
change, remote operation or tag occurs. Historical repositories/evidence remain
unchanged. Q4 is still an intermediate reconstruction: Q5 quantisation awareness
is deliberately absent. Q5 has not begun; no new physical qualification is claimed.
