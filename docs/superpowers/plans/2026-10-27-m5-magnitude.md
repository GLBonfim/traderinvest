# M5 magnitude-weighted supervised experiment

Goal: test one predeclared offline economic-loss weighting hypothesis, without changing approval or execution.
Architecture: reuse economic 51 features, boosted estimator, purged TRAIN hooks and fixed six depth3/5 candidates. Multiply specialty5/1 weights by clipped abs(TRAIN netR) normalized mean1 per pair. Floor0.25R cap3R fixed before market reads. No recency/uniqueness combination or adaptive cap search. Magnitude is supervised target information in TRAIN only, never an input feature. Weighted classifier score is not an unweighted calibrated probability. Existing thresholds/gates remain fixed; this may fail due score semantics or tail emphasis. Reused2024/25 development is exploratory, not independent. 2026 reserved. One run only, no broker or registry.

Tasks:
- [ ] Synthetic RED tests for exact weights, ignored future outcomes, bounds, alignment and invalid eligible targets.
- [ ] Implement m5_magnitude_training wrapper and frozen source/protocol hashes; daily realized diagnostics reuse.
- [ ] Focused tests, Ruff and mypy; independent review.
- [ ] Stop only idle manager via existing supervised session, confirm lock free, append one job and allowlisted offline module, restart same manager root. Preserve H1/collector/cash.
- [ ] Verify worker/protocol pre-data freeze; on completion audit model/csv hashes, costs, DD and counts. Update exports/automation. No candidate promotion without full gates and later validation.
