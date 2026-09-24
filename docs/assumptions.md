# Assumptions register

Every place where ercot-mis does something ERCOT's own engines might not, or leans
on a fact it has not confirmed. The rule: **defaults do what ERCOT does; our analysis
choices are named options.** Each row says where the assumption lives, what ERCOT
does as far as we know, how it was or can be checked, and its status. Numbers live in
the reports the checks write (`data/reports/`), not here. Update a row in the same
commit as the code it describes.

Status: **verified** (checked on real packages, defaults follow ERCOT), **option**
(ERCOT's behaviour is the default and ours is opt-in), **open** (not yet checked),
**external** (needs data we do not pull yet).

## Topology

| assumption | where | ERCOT | check | status |
|---|---|---|---|---|
| CRR bus ties (breakers at the minimum reactance) stay as branches and monitored ones are enforced | `out/network.py` `contract_ties=False` | The auction solves the RAW as given; the monitored CSV lists breakers with real ratings | `scripts/check_network.py`: the uncontracted model factorizes and solves, tie flows are determinate, PTDF rows differ from the contracted ones by small amounts; the contracted model drops the monitored ties' limits | option (contraction is opt-in) |
| Node identity for matching uses the contracted key | `core/node.py` | n/a (our construct) | keys stable across DAM hours and CRR months (identity report) | verified |
| Out-of-service branches are not part of the model | `out/network.py` | DAM README: the RAW incorporates the outage scheduler; CRR outages that name a workbook operations name are all out of service in the RAW | measured on a CRR month; DAM stated by ERCOT | verified |
| The CRR Outages file is informational | `raw/crr.py` parses it; core does not apply it | Same as above: the RAW already reflects it | measured | verified |
| Isolated buses and minor islands are dropped; the largest component is kept | `out/network.py` | The engines must do the same to solve | harmless; counts in `dropped_nodes` | verified |
| The slack is ERCOT's swing bus | `out/network.py` `slack_source` | DAM README names the MMS-DAM slack; the RAW type-3 bus is it. CRR RAW marks one per island | measured; fallback recorded | verified |
| Ambiguous node keys (identical attachments at one station) get an ordinal suffix | `core/node.py` | n/a | those buses are always isolated and never in the kept network | verified, harmless |
| DAM contingency files differ by hour, so each hour is its own snapshot | `core/snapshot.py` | ERCOT posts one per hour | name sets differ across the hours of a day | verified |
| Ties out of service are not contracted even when contraction is on | `core/node.py` | An open breaker separates bus sections | by construction | verified |

## Impedances and ratings

| assumption | where | ERCOT | check | status |
|---|---|---|---|---|
| Reactance and tap are used as written (`CW=1`, `CZ=1`, no phase shifters) | `core/branch.py` | PSS/E conventions | every transformer in both models has `CW=1`, `CZ=1`, `ang1=0` | verified |
| DAM clamps reactance at a floor; comparisons raise both sides to it | `core/match.py` | DAM RAW has no branch below the floor | measured | verified |
| Negative reactances (series capacitors) are kept | `out/network.py` | Both RAWs carry them | present in both | verified |
| DAM base limit is rate A, post-contingency rate B; CRR base is BaseCaseRating, post is EmergencyRating | `out/network.py` | Not stated in the DAM README; ERCOT's NOMCR rating conventions (normal / emergency / 15-minute) | read the ERCOT rating conventions and compare rate B with the CRR emergency rating on matched branches | open |
| A rating of zero means no limit | `out/network.py` | PSS/E convention | a handful of DAM lines | verified as convention, not as ERCOT's reading |
| CRR limits come from the monitored CSV, one time-of-use block; the RAW is PeakWD | `out/network.py` `time_of_use` | The auction clears per block with the block's ratings | blocks identical this month | option |
| DynamicRatings is not applied | `raw/crr.py` (archived) | The workbook holds monthly temperature assumptions, not element ratings; the monitored CSV already carries the resulting ratings | workbook shape inspected | verified |
| Every limited branch is enforced under every contingency | `out/network.py` | Both engines screen and enforce a subset; the published DAM binding constraints are the ones that bound | this is the full model, not the enforced subset; ftr_align decides which it compares | option (documented) |
| DAM enforces the secured set; monitored-only rows are unlimited | `out/network.py` `limits` | CIM `DAM Secured` flag reading, unverified | compare with binding constraints in NP4-191-CD | external |
| Empty contingencies are dropped | `out/network.py` | Removing an absent element changes nothing | by construction; listed in `dropped_contingencies` | verified |

## Names, orientation, constraints

| assumption | where | ERCOT | check | status |
|---|---|---|---|---|
| CRR line names run from RAW `i` to `j` | `core/branch.py` | The comment is "i name j name ckt" | every line this month | verified |
| CRR transformer names follow the `Autos` sheet, which is reversed relative to the RAW for a large minority; flow directions refer to the name | `core/branch.py` `is_name_reversed`, `out/network.py` GTC sign | The CSVs name transformers by the sheet | measured; sign flipped for reversed names | verified |
| GTC member factors are signed From-To positive | `out/network.py` | CSV `FlowDirection` | by construction | verified |
| DAM GTC limits are the GTL workbook's DAM column for the delivery day, latest posting wins | `core/build.py` | The workbook is reposted; the latest posting is ERCOT's final | document IDs do not follow posting time, so the catalog's posting time is used | verified |
| DAM GTC members are not known; the CRR id comes from a manual crosswalk | `core/gtc.py` | NP3-770-M defines them | parse NP3-770-M or borrow CRR members | open |
| Contingency matching is one-to-one | `core/match.py` | CRR splits what DAM lumps in places | many unmatched CRR contingencies sit inside one DAM contingency | open (a `subset` method) |
| Settlement points a contingency disconnects are not special-cased | `raw/dam.py` parses `SpCtg`; core does not use it | The DAM knows which single-bus settlement points a contingency islands | table shape inspected; semantics from the README | open |
| DST days: the extra hour is the third; GTL hour-ending is start hour plus one | `raw/dam.py`, `raw/gtl.py` | README states the DAM convention | verify on the November long day | open |

## Settlement points

| assumption | where | ERCOT | check | status |
|---|---|---|---|---|
| Hub price is the plain average of its buses; the two average hubs are derived from the real ones | `core/settlement_point.py` | Protocols define hubs as bus averages and the average hubs as averages of hubs and of hub buses | cite the section | open (reading) |
| Load zone weights are the loads' MW distribution factors, in-service loads only | `core/settlement_point.py` | Zone price is the load-weighted average | consistent with the `Ld` file | verified as reading |
| CRR hub and zone weights are normalized to one | `core/settlement_point.py` | The CSV gives MW-scale weights | measured | verified |
| CRR hubs and zones span far more buses than the DAM's hub-bus file and load list | `core/diff.py` | Unknown whether definition or resolution | read `diff_settlement_point` rows | open |
| Logical resource nodes take their combined-cycle point's bus | `core/settlement_point.py` | The `Sp` file names it | rows without one stay unresolved | verified |

## Not yet built (the checks that would catch everything above)

- DAM prices, settlement point prices, awards and shadow prices (Public API): the
  sum-to-zero and merchandising-surplus identities are the only external test of the
  whole chain.
- The PSS/E parser has no cross-check against an independent reader.
