# out.network — one snapshot as a DC model reads it

`session.network(snapshot_id, options)` assembles a `Network` from the core tables of
one CRR month or DAM hour: nodes with a dense index, branches with reactance, tap
ratio and two limits, contingencies as sets of branch indexes, GTCs as factor-weighted
member sets, and a list of everything it left out and why. Core stores facts; this
layer applies the judgment calls, each a named option defaulting to ERCOT practice.
It is assembled on first request and kept under `out/network/<snapshot>/<key>/`, one
Parquet file per frame plus `network.json`; the key covers the options and the versions
of the code that built it, so a changed option or a rebuilt core gives a new folder.
A manual override edited without a version bump (the GTC crosswalk) does not: pass
`cache=False` or delete the folder.
Consumer: `ftr_align/cases/ercot.py` builds its incidence matrix from `from_index`,
`to_index` and `x_pu`, its limits from `base_limit_mw` and `contingency_limit_mw`,
and its contingency keys from `branch_indexes`.

## Conventions (facts about the inputs, not options)

- **Reactance and tap are used as written.** Both RAWs give transformer windings as
  per-unit turns ratios (`CW = 1`) and impedances on the system base (`CZ = 1`), so
  `x_pu` is the DC reactance and `tap_ratio = windv1 / windv2` the magnitude tap. No
  phase shifters occur (`ang1 = 0` throughout).
- **A rating of zero is no limit.** PSS/E's convention; the few DAM branches with a
  zero rate A get `+inf`, as does every branch the CRR monitored-element CSV does not
  list.
- **Isolated buses and minor islands are dropped.** Both RAWs carry buses with no
  in-service branch (type 4 and others) and occasionally a small component apart
  from the main grid. A DC model needs one connected component; the largest is kept
  and the rest listed in `dropped_nodes` (`isolated`, `island`) with their branches.
- **Slack is ERCOT's.** The DAM RAW marks one swing bus (type 3). The CRR RAW marks
  one per island, and all but one of those islands are generator terminal buses cut
  off from the grid that month, so after islands are dropped one swing bus remains
  and it is the slack. If several remained the busiest would be taken; if none, the
  busiest node stands in. `slack_source` says which case applied (`ercot` or
  `fallback`), `slack_node_id` and `is_slack` say which node. The choice does not
  change DC flows.
- **Negative reactances** (series capacitors) are kept as written in both models.
- **DAM contingencies** have rows a topology model cannot apply: load, generator and
  settlement-point outages (injection changes, not topology) and split-bus operations
  (topology changes that are not element removals). They are counted per contingency
  (`n_other_rows`, `has_split_bus`) and the branch outages are applied.
- **Empty contingencies are dropped and listed.** A contingency is empty when none
  of the elements it removes is a branch of the network: the equipment is already out
  of service in that month's RAW (the most common case), it is a breaker or switch
  that contraction folded into a node, or the RAW does not know the name. Removing an
  absent element constrains nothing beyond the base case, so the contingency is
  dropped (`drop_empty_contingencies`) and listed in `dropped_contingencies` with its
  counts, the same way dropped branches and nodes are.
- **The audit travels with the network.** `dropped_branches`, `dropped_nodes` and
  `dropped_contingencies` are frames on the `Network`, and `summary()` gives the
  counts by reason. No log file: when `out/` is written to disk the same frames become
  an audit table per snapshot, queryable like any other.
- **GTC member factors are signed** with the branch's RAW orientation. The CSV's
  `To-From` negates, and so does a transformer whose CRR name lists the ends the other
  way round (`is_name_reversed`), because the CSV direction refers to the name. DAM GTC
  rows carry the GTL limit and the crosswalked CRR id but no members until NP3-770-M
  is parsed (docs/datasets/generic-transmission-limits.md).
- **Settlement points ride along.** `settlement_point_nodes` gives each settlement
  point's weights over the kept nodes, renormalized to one, and `settlement_points`
  records the weight that fell on dropped nodes (`weight_dropped`). This is the matrix
  that maps injections at settlement points (awards, bids) onto nodes. Without
  contraction a CRR point's weight is spread equally over its group's buses.
- **Islanding contingencies are not screened here.** Whether removing an outage set
  disconnects the network depends on the consumer's connectivity check, as in
  `ftr_align.network.is_connected`.

## Options (judgment calls)

| option | default | meaning |
|---|---|---|
| `contract_ties` | `False` | ERCOT's topology: every bus is a node and CRR bus ties are branches at their RAW reactance, monitored ones with their breaker ratings. With `True`, buses joined by in-service ties are one node; the ties and any real branch in parallel with a tie group are dropped (`contracted_tie`, `loop`) and their limits with them. The cost of contraction is measured by `scripts/check_network.py`. Tie members keep their group key in their id (`<key>@<bus>`). No effect on DAM. |
| `rating_source` | model default | `crr_monitored` (CRR CSV, per time-of-use block) or `psse_raw` (RAW rate A/B). CRR defaults to the CSV, DAM to the RAW. |
| `time_of_use` | `PeakWD` | The CRR CSV block; the CRR RAW is the PeakWD model. |
| `limits` | `enforced` | Which branches get finite limits: `enforced` (CRR monitored, DAM secured), `monitored` (DAM monitored or secured), `all`. |
| `contingency_rating` | `emergency` | Post-contingency limit: `emergency` (rate B / EmergencyRating) or `base`. |
| `keep_out_of_service` | `False` | Keep branches the RAW marks out of service. |

Every assumption behind these conventions, with its status, is in
[docs/assumptions.md](assumptions.md).

## What is not in the first cut

- Nothing cross-model: a CRR and a DAM `Network` are each in their own vocabulary.
  Putting both on one node set (the `intersection` in `ftr_align` needs it) goes
  through `core.match_bus` and `core.match_branch` and is the next step.
- DAM GTC members (borrowed from the CRR model through `crr_gtc_id`, or parsed).
- Writing to `out/` on disk and the classification-aware `export()`.
- Time-of-use blocks other than the one asked for; DynamicRatings.
