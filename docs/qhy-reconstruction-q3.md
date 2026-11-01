# Q3 FITS representation-tolerance reconstruction — 1 November 2026

Historical source: `0240222451e7f86cf05b0cb12e3132ab5cac3258`.
Branch: `qhy/development`.
Parent Q2: `601951b92d8ba40e672a3172e0178558ecafd200`.
Generic G: `8fbf4612f1be85644c867eb51bbf7b7c16b00b46` (unchanged).

## Application reconstruction

Only `indi_allsky/exposure/qhy5lii_c.py` changes in application source.
Its complete blob matches historical Q3 exactly, with only the installed-tree
`app/` path prefix removed. All eight relevant Q1–Q3 application blobs match
historical Q3. Generic fixes/helper and unrelated application files stay unchanged.

The historical tolerance uses the greater of 2.1 microseconds or half a printed
scientific unit plus epsilon. It admits harmless FITS representation rounding;
requested settings remain authoritative. Gain, binning, CFA, freshness/order,
single-flight association, replay and duplicate checks retain historical
behaviour. Native telemetry gains FITS exposure/gain/date and applied tolerance.
No later clipping-recovery or quantisation correction is introduced.

## Focused tests and fixture portability

The two original Q3 regression methods are carried forward. The original test's
ignored `evidence/failed-feedback-original.fit` dependency is replaced by
`tests/fixtures/qhy-failed-feedback-header.txt`: six selected original header
cards, with trailing padding removed, combined with the existing deterministic
synthetic image generator. All original assertions and the reconstructed request
wall-time boundary remain. No original image pixels or private configuration
are copied into the shared repository. Other historical QHY tests are unchanged.

Separately, the untouched full native FITS in the preserved historical evidence
was read directly. SHA-256 before/after:
`d45397c336e5f6ee329816e9742ab6fc09512bcf37215670fcdffea330808112`.
Q2 rejects this exact native frame for FITS/settings mismatch; reconstructed Q3
accepts it at requested 10.002973 s, reported 10.002970 s, gain 1, with approximately
5 microseconds tolerance. This verifies the original pixels/header too, without
making portable tests depend on them.

Commands from repository root:

```sh
/var/lib/indi-allsky/venv/bin/python3 -B -m unittest tests.test_qhy5lii_c tests.test_adu_precision tests.test_exposure_controller
PYTHONPATH=. /var/lib/indi-allsky/venv/bin/python3 -B tests/check_qhy_integration.py
```

Results: 32 test methods pass (17 QHY, five precision, 10 generic), including
110 parameterized subtests (104 generic plus six Q3 rejection cases). Scientific
formatting is accepted from sub-millisecond through 15-second labels. Wrong
exposure (10 microseconds/1 millisecond), gain, binning, CFA, stale timestamp and
duplicate frames remain rejected. Real ImageWorker/QHY/basic construction,
SQM helper setup and the real INDI writer with in-memory acknowledgements pass.
Syntax compilation and whitespace checks pass.

This is reconstruction validation, not new full processing or camera qualification.
No services, camera connections, production files, historical evidence, remotes
or tags are changed. The host-specific historical deployment document is not
imported. Q4/Q5 remain separate stages; Q4 has not begun. The external
STAGE4_Q3_RESULT_20261101.md records the resulting historical-to-new commit mapping.
