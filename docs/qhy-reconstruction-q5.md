> Historical reconstruction note from `00c29c1450a77694ad46cb1fd755377845664e86`.
> Claims and IDs below describe the original September-based reconstruction.
> Stage 10B carries this stage onto November upstream; current results are in
> the external `STAGE10B_RESULT_20261101.md`. This is not a new physical qualification.

# Q5 short-exposure quantisation reconstruction — 1 November 2026

Historical source: `28ff8541b97e43d1b6ef1a52593b3ce49d5bda2c`.
Branch: `qhy/development`.
Parent Q4: `d4f20f26eb5b9985afcb1340f7b116b09729979c`.
Generic G: `8fbf4612f1be85644c867eb51bbf7b7c16b00b46` (unchanged).

## Exact application reconstruction

The historical Q5 patch is applied to `indi_allsky/capture.py` and
`indi_allsky/exposure/qhy5lii_c.py`, stripping only the installed `app/` prefix.
Both complete blobs match historical Q5 byte-for-byte. No other application
path changes, no refactoring and no additional behavioural changes occur.

This preserves the readout/SDK-specific 262.5 microsecond row model, adjacent-row
probes, unreachable-ADU-band selection/lock, changed-scene reconsideration,
readout/mode/gain/bounds scoping and resolution telemetry exactly. The gate
receives the same readout callback and rejects readout changes during pending
frames. The previous clipping recovery remains part of the historical policy.

## Tests and bounded portability changes

The historical `tests/test_qhy_quantisation.py` is carried with its 14 original
test methods and assertions. Only dependencies are adapted:

- Use package-qualified `tests.test_qhy5lii_c` import.
- Compare against reconstructed Q4 commit
  `d4f20f26eb5b9985afcb1340f7b116b09729979c:indi_allsky/exposure/qhy5lii_c.py`
  instead of the unavailable historical snapshot Git path. Q4 was already
  verified byte-identical to its historical source.
- Use `tests/fixtures/qhy-exposure-quantisation.json`, containing selected
  recorded INDI properties and all 24 boundary-frame exposure/B-plane means.
  Source evidence SHA-256 values are included for provenance. No private paths,
  camera identifiers, full native images or configuration are copied.
- Use a temporary SDK-file stand-in and the original mocked digest for the
  cached-property reader test, avoiding installed-library filesystem dependency.

Existing QHY tests retain the previously reviewed Q3 header-fixture adaptation.
Historical replay scripts, host logs and deployment evidence remain where they
were; they are not bulk-imported or rerun for identical-source reconstruction.

Commands from repository root:

```sh
/var/lib/indi-allsky/venv/bin/python3 -B -m unittest tests.test_qhy_quantisation tests.test_qhy5lii_c tests.test_adu_precision tests.test_exposure_controller
PYTHONPATH=. /var/lib/indi-allsky/venv/bin/python3 -B tests/check_qhy_integration.py
```

All 50 methods pass: 35 QHY/quantisation, five precision-helper and 10 generic,
including 110 subtests (104 generic and six FITS rejection cases). Coverage
includes measured plateaux/boundaries, clipped/unclipped straddles, stable rows,
illumination changes, minimum/bounds, Day/Night/Moon/gain/readout resets, SQM,
region-edge closure, outside-region and unmapped equivalence to Q4, pending
readout-change rejection and telemetry. Existing clipping-recovery regressions
pass. These are offline checks and models, not new physical camera validation.

Real worker/QHY/basic construction, synthetic-mask/SQM setup and real INDI
writer checks with in-memory properties pass. Python syntax and whitespace
checks pass. Committed Q5 source blobs are verified after commit creation.

The external STAGE4_Q5_RESULT_20261101.md records the resulting commit mapping.
Working production, historical repository and evidence remain unchanged. No
services, camera connections, builds, remotes, tags or publication are involved.
Stage 5 full payload/packaging equivalence verification has not begun; Q5 checks
alone are not a claim that that separate stage is complete.
