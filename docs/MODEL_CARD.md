# PHAROS model card

## Intended use

Decision support for a maritime-domain-awareness analyst working **public, open-source AIS
data**. PHAROS surfaces candidate maritime incidents (dark ships, ship-to-ship transfers,
loitering / zone incursions, spoofing, trajectory anomalies) and a composite per-vessel threat
picture for **human review**. It is analytical and defensive.

## Out of scope / prohibited

- **Not an automated verdict.** An incident is a flagged *pattern in public broadcast data*, not
  a determination of illicit activity. A dark-ship gap is frequently a benign AIS receiver-coverage
  loss; a rendezvous may be a legitimate bunkering or pilot transfer. Every incident carries a
  reliability grade and its transparent evidence for exactly this reason.
- No targeting of individuals; PHAROS reasons about vessels (MMSI), not persons.
- No non-public, authentication-gated, or purchased data; no offensive capability.

## Data & signals

- **AIS is self-reported and gappy.** Identity fields (name, flag, type) can be absent or
  falsified — the spoofing detector exists because they cannot be trusted at face value. Coverage
  is uneven (dense terrestrial near shore, sparse satellite offshore), which is the dominant
  confound behind "dark ship" false positives.
- **Reliability grading.** Incidents get a NATO Admiralty-style grade (A completely reliable … F
  cannot be judged) reflecting AIS confidence: corroborated events in dense coverage grade higher
  than lone signals in sparse coverage.

## Evaluation & known limitations

**Trajectory anomaly flagship:** An 8-unit GRU sequence autoencoder over ordered per-step tracks.
Under unsupervised training (no labels), it achieves 0.962 within-region / 0.962 cross-region AUC,
ahead of Isolation Forest (0.940 / 0.942), while linear PCA falls below chance (0.273). A 25-run
capacity sweep selected 8 over 64 hidden units (0.963 / 0.964 mean AUC), reducing 38,660 parameters
to 804. Normalization is fitted on the training partition only; the earlier 0.971 / 0.967 result
was retired after a validation-leakage audit. This is the best measured fit for PHAROS's small
whole-track corpus, not a universal state-of-the-art claim; real trajectory-anomaly labels are
still unavailable.

**Training/inference integrity:** Batch training atomically persists versioned weights,
train-partition normalization, the calibrated threshold, and provenance. `POST /score-track` loads
that exact artifact. If it is absent or invalid, the endpoint uses an explicit runtime fallback and
returns its `model_source`, avoiding silent training/inference drift. Artifacts load through
PyTorch's restricted `weights_only` path and contain tensors and primitive metadata only.

**Synthetic evaluation ceiling:** The offline gold set's near-perfect detector precision/recall
(~1.0 for rendezvous, loiter, gaps) is a *known ceiling* — self-generated anomalies are separable
by construction. That is not a capability claim; it validates the harness and trap methodology.

**Real-data validation:** The honest test is NOAA Marine Cadastre AIS. Real data exposed
catastrophic false positives in congested ports (naive rendezvous / loiter: 2,999 incidents);
three domain-correct fixes reduced them ~98% (rendezvous 2,892 → 8, total 2,999 → 51). The GRU
trained on real pattern-of-life surfaces interpretable outliers (e.g., Catalina Island high-speed
ferries). Real AIS carries no anomaly labels — that lane is qualitative. See [`EVAL.md`](EVAL.md).

**Known limitation:** The AIS coverage confound is dominant behind "dark ship" false positives
(dense coverage near shore vs. sparse offshore). Measured against Global Fishing Watch
reception-modelled gap events rather than assumed away. The first real LA/LB cross-check returned
no candidates. A selected east-Gulf cohort subsequently corroborated 4/34 PHAROS rendezvous and
65/298 loiter calls at vessel/type/time/place level, but these are not precision estimates because
the cohort is label-enriched and GFW is incomplete. Its gap label was 169–209 km offshore and had
no NOAA reappearance report, so gap calibration remains unestimable. Recorded negatives are kept,
not buried.

**Scale validation:** The full 1,982-vessel east-Gulf slice is now tractable. A conservative
slow-motion space/time index retains 51,570 of 937,765 possible rendezvous pairs (5.50%) before
unchanged exact scoring; the detector completes in 7.12s after CSV loading. It returns 186
symmetric calls (93 pairs), of which 3 agree with the four GFW encounter labels under the existing
vessel/type/time/place rule. This 1.6% agreement is not precision because GFW is incomplete.

## Provenance

Free/open data only: NOAA Marine Cadastre (bulk historical AIS), AISStream.io (live), Global
Fishing Watch (event labels), Natural Earth / EEZ reference geometry.
