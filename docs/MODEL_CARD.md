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

**Trajectory anomaly flagship:** A GRU sequence autoencoder over ordered per-step tracks.
Under unsupervised training (no labels), it achieves ~0.96 AUC while the linear PCA baseline falls
below chance (~0.27 AUC). The recurrent depth is necessary, not decorative.

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
reception-modelled gap events rather than assumed away. Recorded negatives are kept, not buried.

## Provenance

Free/open data only: NOAA Marine Cadastre (bulk historical AIS), AISStream.io (live), Global
Fishing Watch (event labels), Natural Earth / EEZ reference geometry.
