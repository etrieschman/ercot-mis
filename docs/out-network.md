# out.network — one snapshot as a DC model reads it

`session.network(snapshot_id, options)` assembles a `Network` from the core tables of
one CRR month or DAM hour: nodes with a dense index, branches with reactance, tap
ratio and two limits, contingencies as sets of branch indexes, GTCs as factor-weighted
member sets, and a list of everything it left out and why. Core stores facts; this
layer applies the judgment calls, each a named option defaulting to ERCOT practice.
It is computed on demand and not written to disk yet (`export()` will do that).
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
- **Slack.** The DAM RAW marks one slack bus; the CRR RAW marks several (type 3). The
  slack node is the type-3 node with the most branches, then the highest voltage, else
  the busiest node. The choice does not change DC flows; it is recorded in
  `slack_node_id` and `is_slack`.
- **Negative reactances** (series capacitors) are kept as written in both models.
- **DAM contingencies** have rows a topology model cannot apply: load, generator and
  settlement-point outages (injection changes, not topology) and split-bus operations
  (topology changes that are not element removals). They are counted per contingency
  (`n_other_rows`, `has_split_bus`) and the branch outages are applied.
- **CRR breaker outages** vanish under contraction: a tie inside a contracted node is
  not a branch of the network, so the contingency's `n_dropped` counts it and the
  contingency may come out empty (`is_empty`). Empty contingencies are kept so the
  consumer can decide to skip or report them.
- **GTC member factors are signed** with the branch's from-to orientation (`To-From`
  negates). DAM GTC rows carry the GTL limit and the crosswalked CRR id but no
  members until NP3-770-M is parsed (docs/datasets/generic-transmission-limits.md).
- **Islanding contingencies are not screened here.** Whether removing an outage set
  disconnects the network depends on the consumer's connectivity check, as in
  `ftr_align.network.is_connected`.

## Options (judgment calls)

| option | default | meaning |
|---|---|---|
| `contract_ties` | `True` | CRR: buses joined by in-service bus ties are one node; the ties and any real branch in parallel with a tie group are dropped (`contracted_tie`, `loop`). With `False`, tie members are their own nodes (`<key>@<bus>`) and ties are branches at their RAW reactance. No effect on DAM. |
| `rating_source` | model default | `crr_monitored` (CRR CSV, per time-of-use block) or `psse_raw` (RAW rate A/B). CRR defaults to the CSV, DAM to the RAW. |
| `time_of_use` | `PeakWD` | The CRR CSV block; the CRR RAW is the PeakWD model. |
| `limits` | `enforced` | Which branches get finite limits: `enforced` (CRR monitored, DAM secured), `monitored` (DAM monitored or secured), `all`. |
| `contingency_rating` | `emergency` | Post-contingency limit: `emergency` (rate B / EmergencyRating) or `base`. |
| `keep_out_of_service` | `False` | Keep branches the RAW marks out of service. |

## What is not in the first cut

- Nothing cross-model: a CRR and a DAM `Network` are each in their own vocabulary.
  Putting both on one node set (the `intersection` in `ftr_align` needs it) goes
  through `core.match_node` and `core.match_branch` and is the next step.
- DAM GTC members (borrowed from the CRR model through `crr_gtc_id`, or parsed).
- Writing to `out/` on disk and the classification-aware `export()`.
- Time-of-use blocks other than the one asked for; DynamicRatings.
