# SG-PILOT-v0 freeze record

## Status

**Freeze candidate prepared on 2026-07-16; Pilot Day 0 has not started yet.** Day 0 begins only
after the Phase 1 gate passes, this implementation is committed/pushed, the launch agent is
loaded, and a valid Singapore AIS report is durably recorded. The implementation commit and
collection start time will be added at that point.

The freeze covers the artifact bytes, anomaly threshold, and deterministic detector configuration
below. Collector, review, evaluation, snapshot, and dashboard tooling may evolve without changing
the scoring freeze. Any scoring change creates a new freeze for a later holdout; it does not replace
this pilot artifact during the window.

## Frozen anomaly artifact

| Field | Value |
|---|---|
| local path (gitignored) | `data/models/gru-sequence-anomaly.pt` |
| SHA-256 | `01faa27e17dd194b8913a439034a5f71d56f48ebb291ddb073e6c9b0ee7788fb` |
| model | GRU sequence autoencoder |
| hidden units / parameters | 8 / 804 |
| input | 15 ordered steps × 4 motion features |
| calibration | 99th percentile over the training corpus |
| frozen raw threshold | `8.867941474914566` |
| seed | 0 |
| normalization | fitted on the training partition only |
| trained at | `2026-07-16T02:31:34.025232+00:00` |

The 8-unit GRU remains the best supported PHAROS model for this use case: its recorded multi-seed
within/cross-region AUC is stronger than Isolation Forest and PCA, while remaining small enough for
an M3 Pro background workload. This is model selection evidence, not real Singapore anomaly
precision/recall. Architecture or threshold promotion waits for independent Singapore labels and
a later frozen holdout.

## Training corpus

The artifact was fitted on 881 tracks / 689 vessels / 1,144 segmented voyages from two local,
real NOAA AIS backgrounds:

| Region | Reports | Input SHA-256 |
|---|---:|---|
| LA / Long Beach (`us-la`) | 145,028 | `fe50cd68a399926d199c8b620f9baa16d257ddab9836f0b24116c81560f8670a` |
| east-Gulf selected cohort (`us-gulf`) | 173,204 | `8a55c2053871301f6188c64a70b165f4eb21585a438cb350f4e2ca72815f991e` |

The Gulf cohort is deliberately label-enriched and therefore not a prevalence sample. Combining it
with congested-port LA/LB traffic broadens the unsupervised background, but independent Singapore
review remains necessary. The ignored preparation database is `data/sg-pilot-training.db` (96 MB
after preparation). Reproduce the artifact with:

```bash
PYTHONPATH=src make prepare-pilot-model
```

Reproduction writes a new artifact and therefore requires an explicit freeze review; do not run it
on the pilot artifact during days 0–14.

## Frozen deterministic detector configuration

| Detector setting | Value |
|---|---:|
| AIS gap minimum duration | 120 minutes |
| AIS gap minimum displacement | 5 km |
| rendezvous maximum separation | 0.5 km |
| rendezvous maximum participant speed | 1 kn |
| rendezvous minimum duration | 30 minutes |
| rendezvous minimum evidence of transit | 3 kn |
| rendezvous maximum partner degree | 2 |
| loiter radius | 2 km |
| loiter minimum duration | 60 minutes |
| loiter speed range | 0.5–2 kn |
| spoof implied-speed threshold | 60 kn |
| anomaly sequence length | 16 resampled points / 15 steps |
| anomaly threshold percentile | 99 |

Operational settings such as the 45-second batch/downsample cadence may be adjusted for laptop
health and storage without tuning model/detector thresholds. Every evaluation row will carry the
artifact SHA and this freeze version.
