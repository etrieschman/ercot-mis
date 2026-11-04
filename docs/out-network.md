# out.network — one snapshot as a DC model reads it

`session.network(snapshot_id, options)` assembles a `Network` from the core tables of
one CRR month or DAM hour: buses with a dense index, branches with reactance, tap
ratio and two limits, contingencies as sets of branch indexes, GTCs as factor-weighted
member sets, and a list of everything it left out and why. The rows are buses because a
solver's model is bus-branch: a DAM bus is a RAW record; a CRR bus is, by default, one
node (ERCOT's auction keeps the closed breakers as branches), or with `contract_ties` the
nodes joined by closed breakers. Core stores facts; this layer applies ERCOT's practice,
with the two judgment calls a check varies as options.
It is assembled from the core tables on every call (well under a second; a saved copy
read slower than assembling, so none is kept).
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
- **Isolated buses and minor islands are dropped.** Both RAWs carry records with no
  in-service branch (type 4 and others) and occasionally a small component apart
  from the main grid. A DC model needs one connected component; the largest is kept
  and the rest listed in `dropped_buses` (`isolated`, `island`) with their branches.
- **Slack is ERCOT's.** The DAM RAW marks one swing bus (PSS/E type 3). The CRR RAW marks
  one swing node per island, and all but one of those islands are generator terminals cut
  off from the grid that month, so after islands are dropped one remains and it is the
  slack. If several remained the busiest would be taken; if none, the busiest bus stands
  in. `slack_source` says which case applied (`ercot` or `fallback`), `slack_bus_id` and
  `is_slack` say which bus. The choice does not change DC flows.
- **Negative reactances** (series capacitors) are kept as written in both models.
- **DAM contingencies** have rows beyond branch outages: load, generator and
  settlement-point outages (injection changes, not topology; counted in `n_other_rows`)
  and split-bus operations, which move one end of a branch to a new bus section. The
  branch outages are applied, and the split-bus moves travel with the contingency
  (`split_branch_indexes`, `split_ends`) for the solver to apply (NAM-09).
- **Empty contingencies are dropped and listed.** A contingency is empty when none
  of the elements it removes is a branch of the network and it moves no bus section: the equipment is already out
  of service in that month's RAW (the most common case), it is a breaker or switch
  that contraction folded into a bus, or the RAW does not know the name. Removing an
  absent element constrains nothing beyond the base case, so the contingency is
  dropped and listed in `dropped_contingencies` with its
  counts, the same way dropped branches and buses are.
- **The audit travels with the network.** `dropped_branches`, `dropped_buses` and
  `dropped_contingencies` are frames on the `Network`, and `summary()` gives the
  counts by reason. No log file: when `out/` is written to disk the same frames become
  an audit table per snapshot, queryable like any other.
- **GTC member factors are signed** with the branch's RAW orientation. The CSV's
  `To-From` negates, and so does a transformer whose CRR name lists the ends the other
  way round (`is_name_reversed`), because the CSV direction refers to the name. DAM GTC
  rows carry the GTL limit and the crosswalked CRR id but no members until NP3-770-M
  is parsed (docs/datasets/generic-transmission-limits.md).
- **Settlement points ride along.** `settlement_point_buses` gives each settlement
  point's weights over the kept buses, renormalized to one, and `settlement_points`
  records the weight that fell on dropped buses (`weight_dropped`). This is the matrix
  that maps injections at settlement points (awards, bids) onto buses. With CRR ties
  kept a point's weight is spread equally over the nodes of its bus (SP-06).
- **Islanding contingencies are not screened here.** `ercot_mis.out.shift_factors.DcSystem.outaged`
  finds the part cut off from the slack when it solves a contingency and reports it
  (`n_islanded`, `connected`); the network itself keeps every contingency.

## Options (the judgment calls a check varies)

| option | default | meaning |
|---|---|---|
| `contract_ties` | `False` | ERCOT's topology: one row per CRR node, and CRR ties are branches at their RAW reactance, monitored ones with their breaker ratings. With `True`, one row per `core.bus` row (nodes joined by in-service ties); the ties and any real branch in parallel with a tie group are dropped (`contracted_tie`, `loop`) and their limits with them. The cost of contraction is measured by `checks/network.py`. Tie members keep their group key in their id (`<key>@<bus>`). No effect on DAM. |
| `time_of_use` | `PeakWD` | The CRR CSV block; the CRR RAW is the PeakWD model. |

Everything else the assembly decides is ERCOT's practice and is fixed in code: the
rating source is the model's own (CRR monitored CSV, DAM RAW rate A/B; RAT-04), limits
go to the branches ERCOT enforces (CRR monitored, DAM secured; RAT-08), the
post-contingency limit is rate B (RAT-04), out-of-service branches are not part of the
model (TOP-03) and empty contingencies are dropped and listed (RAT-10).

Every assumption behind these conventions, with its status, is in
[docs/assumptions.md](assumptions.md).

## Not built

- Nothing cross-model: a CRR and a DAM `Network` are each in their own vocabulary.
  Putting both on one bus set goes through `core.match_bus` and `core.match_branch`.
- DAM GTC members: the DAM package carries none; the price check borrows the CRR
  model's for the month, and ERCOT's own definitions (NP3-770-M, PDFs) are not parsed.
- The classification-aware `export()`.
- Time-of-use blocks other than the one asked for; DynamicRatings.
