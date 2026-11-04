# out.network: one snapshot as a DC model reads it

`session.network(snapshot_id, options)` assembles a `Network` from the core tables of one
CRR month or DAM hour: buses with a dense index, branches with reactance, tap ratio and
two limits, contingencies as sets of branch indexes, GTCs as factor-weighted member sets,
settlement point weights over the buses, and a list of everything it left out and why.
Core stores facts; this layer applies ERCOT's practice. The two judgment calls a check
varies are its options. A network is assembled on every call, in well under a second.

The rows are buses because a solver's model is bus-branch. A DAM bus is a RAW record. A
CRR bus is, by default, one node, because ERCOT's auction keeps the closed breakers as
branches; with `contract_ties` it is the nodes those breakers join (`core.bus`).

Consumers build their incidence matrix from `from_index`, `to_index` and `x_pu`, their
limits from `base_limit_mw` and `contingency_limit_mw`, and their contingency keys from
`branch_indexes`. `out.shift_factors.DcSystem` is the solver in this repo.

## Frames

- `buses`: `index`, `bus_id`, `bus_key`, `raw_name`, `substation`, `kv`, `n_nodes`,
  `node_number`, `bus_number`, `is_slack`. For a CRR network with ties kept, `bus_id` is
  the bus key, or `<bus_key>@<node_number>` for a node in a tie group, and `bus_key`
  repeats across the group.
- `branches`: `index`, `branch_id`, `kind`, `from_bus_id`, `to_bus_id`, `from_index`,
  `to_index`, `x_pu`, `tap_ratio`, `base_limit_mw`, `contingency_limit_mw`, `is_limited`,
  `is_monitored`, `is_secured`.
- `contingencies`: `contingency_id`, `branch_ids`, `branch_indexes`, `n_outages`,
  `n_dropped`, `n_unresolved`, `n_other_rows`, `has_split_bus`, `split_branch_indexes`,
  `split_ends`, `n_split_unapplied`, `is_empty`.
- `gtcs` and `gtc_members`: `gtc_id`, `source`, `limit_mw`, `n_members`, `n_unresolved`,
  `crr_gtc_id`; and per member `branch_id`, `branch_index`, `factor` signed in the
  branch's RAW orientation.
- `settlement_points` and `settlement_point_buses`: `settlement_point_id`, `kind`,
  `n_buses`, `weight_dropped`; and per bus `bus_id`, `bus_index`, `weight`, summing to one
  per point.
- `dropped_branches`, `dropped_buses`, `dropped_contingencies`: each with its `reason`.
  `summary()` gives the counts by reason.

## Conventions

Each convention is ERCOT's practice as its documents give it; the register row names the
source and the check that measures it.

- **Reactance and tap are used as written.** Both RAWs give transformer windings as
  per-unit turns ratios and impedances on the system base, so `x_pu` is the DC reactance
  and `tap_ratio` the magnitude tap. No phase shifters occur; a RAW with one fails the
  core build (RAT-01, RAT-11).
- **A rating of zero is no limit** (RAT-05). PSS/E's convention: a branch with a zero
  rate A, and every branch the CRR monitored-element CSV does not list, gets an infinite
  limit.
- **Limits go to the branches ERCOT enforces** (RAT-08): CRR monitored, DAM secured. The
  base-case limit is the CRR `BaseCaseRating` or the DAM rate A; the post-contingency
  limit is the CRR `EmergencyRating` or the DAM rate B (RAT-04). Each model's own ratings.
- **Out-of-service branches are not part of the model** (TOP-03). The DAM README says the
  RAW already includes scheduled outages.
- **Isolated buses and minor islands are dropped** (TOP-05). Both RAWs carry records with
  no in-service branch and sometimes a small component apart from the main grid. A DC
  model needs one connected component; the largest is kept and the rest listed in
  `dropped_buses` with their branches.
- **The slack is ERCOT's** (TOP-06). The DAM RAW marks one swing bus (PSS/E type 3). The
  CRR RAW marks one swing node per island, and the islands other than the main grid are
  dropped, so one remains. If several remained the busiest would be taken; if none, the
  busiest bus stands in. `slack_source` says which case applied. The choice does not
  change DC flows.
- **Negative reactances** (series capacitors) are kept as written (RAT-03).
- **DAM contingencies** have rows beyond branch outages: load, generator and
  settlement-point rows (injection changes, counted in `n_other_rows`) and split-bus
  operations, which move one end of a branch to a new bus section. Branch outages are
  applied; split-bus moves travel with the contingency (`split_branch_indexes`,
  `split_ends`) for the solver to apply (NAM-09).
- **Empty contingencies are dropped and listed** (RAT-10). A contingency is empty when it
  removes no branch of the network and moves no bus section: its equipment is out of
  service in that snapshot, it is a breaker contraction folded into a bus, or the RAW does
  not know the name. Removing an absent element constrains nothing beyond the base case.
- **GTC member factors are signed** in the branch's RAW orientation (NAM-03). The CSV's
  `To-From` negates, and so does a transformer whose CRR name lists the ends the other
  way round (`is_name_reversed`), because the CSV direction refers to the name. DAM GTC
  rows carry the GTL limit and the crosswalked CRR id but no members
  (generic-transmission-limits.md).
- **Settlement points ride along** (SP-06). `settlement_point_buses` gives each point's
  weights over the kept buses, renormalized to one, and `settlement_points` records the
  weight that fell on dropped buses. A weight is given per `bus_key`; with CRR ties kept
  it is spread equally over the nodes of the bus.
- **Islanding contingencies are not screened here.** `DcSystem.outaged` finds the part cut
  off from the slack when it solves a contingency and reports it; the network keeps every
  contingency.

## Options

| option | default | meaning |
|---|---|---|
| `contract_ties` | `False` | ERCOT's topology: one row per CRR node, the ties as branches at their RAW reactance, the monitored ones with their breaker ratings (TOP-01). With `True`, one row per `core.bus` row; the ties and any real branch in parallel with a tie group are dropped (`contracted_tie`, `loop`) and their limits with them. `checks/network.py` measures the cost. No effect on DAM. |
| `time_of_use` | `PeakWD` | The CRR monitored-element CSV block the limits come from; the CRR RAW is the PeakWD model (RAT-06). |

## Not built

- Nothing cross-model: a CRR and a DAM `Network` are each in their own vocabulary.
  Putting both on one bus set goes through `core.match_bus` and `core.match_branch`.
- DAM GTC members: the package carries none, and NP3-770-M is not read.
- Time-of-use blocks other than the one asked for; dynamic ratings.
