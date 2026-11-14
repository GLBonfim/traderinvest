# T3 run records

Byte-identical copies of the T3 outputs written by `uv run python -m app.trend.cli t3` on
2026-11-14 into `data/trend/results/t3/` (which stays outside Git). `SHA256SUMS` holds the hash
of each file. `.gitattributes` keeps the bytes unchanged (no line-ending conversion).

- `results.json`: every replay (development and validation × 3 cost scenarios × 5 capitals ×
  strategy and 3 benchmarks). It holds the metrics, the reproduction hash of each run, the
  bootstrap intervals, the informative gates, the minimum-viable-capital table, the manifest
  hash and the code state.
- `trades_<partition>_<capital>.csv`: every strategy order in the realistic scenario, with its
  reason, state, volatility, weights, fill and costs.
- `summary.json`: the same as `results.json` without the trade lists.

The owner report is `docs/trend/t3-desenvolvimento-validacao.md`. It was written before these
copies were added, so where it says the trade lists are outside Git, read: the originals are in
`data/trend/results/t3/`, and these committed copies are byte-identical.
