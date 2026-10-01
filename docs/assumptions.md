# Assumptions register

Every place where ercot-mis has to decide something: where it does what ERCOT's own
engines might not, or leans on a fact it has not confirmed. The rule: **defaults do
what ERCOT does; our analysis choices are named options.** Numbers live in the reports
the checks write (`data/reports/`), not here. Update a row in the same commit as the
code it describes.

What does **not** belong here: differences between two ERCOT models (a load in service
in DAM and out of service in CRR, a rating that differs). Those are observations, not
choices; they go in the `core.diff_*` tables and we change nothing.

Each row has an **id** that the code cites: every field of `Options`, every reason an
element is dropped from `out.network` and every `match_method` names its row
(`REGISTER_ROWS` in `out/network.py` and `core/match.py`), and `tests/test_register.py`
fails when one does not, or names a row that is not in this file.

**Type** says why the row exists:

- `stated`: ERCOT says so (a README, a protocol section).
- `measured`: we checked it on real packages.
- `forced`: the data is ambiguous or silent and we had to pick.
- `option`: ERCOT does one thing by default and we offer another by name.

**Status**: `verified` (checked on real packages, defaults follow ERCOT), `open` (not
yet checked), `external` (needs data we do not pull yet).

## Topology

| id | type | assumption | where | ERCOT | check | status |
|---|---|---|---|---|---|---|
| TOP-01 | option | CRR bus ties (breakers at the minimum reactance) stay as branches and monitored ones are enforced | `out/network.py` `contract_ties=False` | The auction solves the RAW as given; the monitored CSV lists breakers with real ratings | `scripts/check_network.py`: the uncontracted model factorizes and solves, tie flows are determinate, PTDF rows differ from the contracted ones by small amounts; the contracted model drops the monitored ties' limits | verified (contraction is opt-in) |
| TOP-02 | forced | Bus identity for matching uses the contracted key | `core/node.py` | n/a (our construct) | keys stable across DAM hours and CRR months (identity report) | verified |
| TOP-03 | measured | Out-of-service branches are not part of the model | `out/network.py` | DAM README: the RAW incorporates the outage scheduler; CRR outages that name a workbook operations name are all out of service in the RAW | measured on a CRR month; DAM stated by ERCOT | verified |
| TOP-04 | measured | The CRR Outages file is informational | `raw/crr.py` parses it; core does not apply it | Same as above: the RAW already reflects it | measured | verified |
| TOP-05 | forced | Isolated buses and minor islands are dropped; the largest component is kept | `out/network.py` | The engines must do the same to solve | harmless; counts in `dropped_nodes` | verified |
| TOP-06 | stated | The slack is ERCOT's swing bus | `out/network.py` `slack_source` | DAM README names the MMS-DAM slack; the RAW type-3 bus is it. CRR RAW marks one per island | measured; fallback recorded | verified |
| TOP-07 | forced | Ambiguous bus keys (identical attachments at one substation) get an ordinal suffix | `core/node.py` | n/a | those buses are always isolated and never in the kept network | verified (harmless) |
| TOP-08 | measured | DAM contingency files differ by hour, so each hour is its own snapshot | `core/snapshot.py` | ERCOT posts one per hour | name sets differ across the hours of a day | verified |
| TOP-09 | forced | Ties out of service are not contracted even when contraction is on | `core/node.py` | An open breaker separates bus sections | by construction | verified |

## Impedances and ratings

| id | type | assumption | where | ERCOT | check | status |
|---|---|---|---|---|---|---|
| RAT-01 | measured | Reactance and tap are used as written (`CW=1`, `CZ=1`, no phase shifters) | `core/branch.py` | PSS/E conventions | every transformer in both models has `CW=1`, `CZ=1`, `ang1=0` | verified |
| RAT-02 | measured | DAM clamps reactance at a floor; comparisons raise both sides to it | `core/match.py` | DAM RAW has no branch below the floor | measured | verified |
| RAT-03 | measured | Negative reactances (series capacitors) are kept | `out/network.py` | Both RAWs carry them | present in both | verified |
| RAT-04 | forced | DAM base limit is rate A, post-contingency rate B; CRR base is BaseCaseRating, post is EmergencyRating | `out/network.py` | Not stated in the DAM README; ERCOT's NOMCR rating conventions (normal / emergency / 15-minute) | read the ERCOT rating conventions and compare rate B with the CRR emergency rating on matched branches | open |
| RAT-05 | forced | A rating of zero means no limit | `out/network.py` | PSS/E convention | a handful of DAM lines | verified (as convention, not as ERCOT's reading) |
| RAT-06 | option | CRR limits come from the monitored CSV, one time-of-use block; the RAW is PeakWD | `out/network.py` `time_of_use` | The auction clears per block with the block's ratings | blocks identical this month | verified |
| RAT-07 | measured | DynamicRatings is not applied | `raw/crr.py` (archived) | The workbook holds monthly temperature assumptions, not element ratings; the monitored CSV already carries the resulting ratings | workbook shape inspected | verified |
| RAT-08 | option | Every limited branch is enforced under every contingency | `out/network.py` | Both engines screen and enforce a subset; the published DAM binding constraints are the ones that bound | this is the full model, not the enforced subset; ftr_align decides which it compares | verified (documented) |
| RAT-09 | forced | DAM enforces the secured set; monitored-only rows are unlimited | `out/network.py` `limits` | CIM `DAM Secured` flag reading, unverified | compare with binding constraints in NP4-191-CD | external |
| RAT-10 | forced | Empty contingencies are dropped | `out/network.py` | Removing an absent element changes nothing | by construction; listed in `dropped_contingencies` | verified |

## Names, orientation, constraints

| id | type | assumption | where | ERCOT | check | status |
|---|---|---|---|---|---|---|
| NAM-01 | measured | CRR line names run from RAW `i` to `j` | `core/branch.py` | The comment is "i name j name ckt" | every line this month | verified |
| NAM-02 | measured | CRR transformer names follow the `Autos` sheet, which is reversed relative to the RAW for a large minority; flow directions refer to the name | `core/branch.py` `is_name_reversed`, `out/network.py` GTC sign | The CSVs name transformers by the sheet | measured; sign flipped for reversed names | verified |
| NAM-03 | stated | GTC member factors are signed From-To positive | `out/network.py` | CSV `FlowDirection` | by construction | verified |
| NAM-04 | forced | DAM GTC limits are the GTL workbook's DAM column for the delivery day, latest posting wins | `core/build.py` | The workbook is reposted; the latest posting is ERCOT's final | document IDs do not follow posting time, so the catalog's posting time is used | verified |
| NAM-05 | forced | DAM GTC members are not known; the CRR id comes from a manual crosswalk | `core/gtc.py` | NP3-770-M defines them; the DAM names a binding GTC by the CRR code | `scripts/check_prices.py` borrows the CRR members through `core.match_branch` and prices the binding GTCs with them; not yet in `out.network` | open (borrowing measured) |
| NAM-06 | forced | Contingency matching is one-to-one | `core/match.py` | CRR splits what DAM lumps in places | many unmatched CRR contingencies sit inside one DAM contingency | open (a `subset` method) |
| NAM-07 | forced | Settlement points a contingency disconnects are not special-cased | `raw/dam.py` parses `SpCtg`; core does not use it | The DAM knows which single-bus settlement points a contingency islands | table shape inspected; semantics from the README | open |
| NAM-09 | forced | Split-bus rows of a DAM contingency are counted (`has_split_bus`), not applied | `core/contingency.py`, `out/network.py` | They change topology under the contingency (PRC-05) | `scripts/check_prices.py` applies them and the price residual falls | open (apply in `out.network`) |
| NAM-08 | stated | DST days: the extra hour is the third; GTL hour-ending is start hour plus one | `raw/dam.py`, `raw/gtl.py` | README states the DAM convention | verify on the November long day | open |

## Settlement points

| id | type | assumption | where | ERCOT | check | status |
|---|---|---|---|---|---|---|
| SP-01 | forced | Hub price is the plain average of its buses; the two average hubs are derived from the real ones | `core/settlement_point.py` | Protocols define hubs as bus averages and the average hubs as averages of hubs and of hub buses | cite the section | open (reading) |
| SP-02 | forced | Load zone weights are the loads' MW distribution factors, in-service loads only | `core/settlement_point.py` | Zone price is the load-weighted average | consistent with the `Ld` file | verified (as reading) |
| SP-03 | measured | CRR hub and zone weights are normalized to one | `core/settlement_point.py` | The CSV gives MW-scale weights | measured | verified |
| SP-04 | measured | CRR hubs and zones span far more buses than the DAM's hub-bus file and load list | `core/diff.py` | Unknown whether definition or resolution | read `diff_settlement_point` rows | open |
| SP-05 | stated | Logical resource nodes take their combined-cycle point's bus | `core/settlement_point.py` | The `Sp` file names it | rows without one stay unresolved | verified |

## Matching

| id | type | assumption | where | ERCOT | check | status |
|---|---|---|---|---|---|---|
| MAT-01 | forced | A CRR branch is its DAM branch when the workbook's operations name equals the DAM name once punctuation and case are ignored (`exact`), or followed by the CRR circuit id (`ops+ckt`) | `core/match.py` | No shared key is published; the workbook gives the operations name, DAM appends its own circuit designator | `scripts/measure_identity.py`: rows per method; kind, voltage and reactance of matched pairs | verified (agreement measured; no independent key exists) |
| MAT-02 | forced | Otherwise the one DAM name that is the operations name plus at most two characters (`prefix`); among several, the one with the same reactance (`prefix+x`) | `core/match.py` | Same as above | `scripts/measure_identity.py`: reactance agreement by method | verified (agreement measured; no independent key exists) |
| MAT-03 | forced | Nodes match through a settlement point both models place on one node (`settlement_point`), then by the vote of matched branch endpoints (`branch_endpoints`); never by bus number or name | `core/match.py` | CRR and DAM bus numbers are unrelated and DAM renumbers every hour | `scripts/measure_identity.py`: rows per method; voltage of matched nodes | verified (agreement measured; no independent key exists) |
| MAT-04 | forced | Contingencies match by name (`name`), then by an identical set of translated branches (`members`) | `core/match.py` | Names are shared for most; member sets differ and the difference is recorded | `scripts/measure_identity.py`: rows per method; why name-matched member sets differ | verified (agreement measured; no independent key exists) |
| MAT-05 | forced | Anything no method settles stays `unmatched` and is output; nothing is guessed | `core/match.py` | n/a | by construction; unmatched rows counted in the identity report | verified |

## Prices (the external test)

| id | type | assumption | where | ERCOT | check | status |
|---|---|---|---|---|---|---|
| PRC-01 | measured | A DAM price is the system price minus shadow price times shift factor, summed over the binding rows; no loss term | `scripts/check_prices.py` | ERCOT prices carry no marginal loss component | the residual at every settlement point, per DAM hour; both signs tried | verified (hours without a binding GTC reproduce to a small residual; hours with one are PRC-04) |
| PRC-02 | measured | A binding row's direction is its from and to substation; inside one substation, the two voltages to the tenth of a kV | `scripts/check_prices.py` | The published flow is unsigned; the tenths digit of base kV tells bus sections apart | every binding branch row resolves to a direction; reading the voltage to the whole kV put one line the wrong way round and showed as the largest residual | verified |
| PRC-03 | forced | Under a contingency that cuts nodes off, the part still connected to the slack is solved and the rest gets no shift factor | `scripts/check_prices.py` | Unknown; the `SpCtg` file lists the settlement points a contingency islands (NAM-07) | residual at the points a binding contingency cuts off, reported apart from the rest | open |
| PRC-04 | forced | A binding GTC is priced with the CRR model's members for the same month, plus members kept by hand in `data/overrides/dam_gtc_members.csv` | `scripts/check_prices.py` | The DAM's own definition is not published as data (NP3-770-M is documents: PDF and slides) | across the hours a GTC binds, the residual is one fixed pattern times its shadow price; the pattern is regressed on nearby branches' shift factors. For the GTC that binds in the evening this gave two parallel lines at factor one beyond the CRR's two members; a smaller part of the pattern is still unexplained | open (members inferred from prices, not confirmed by ERCOT) |
| PRC-05 | measured | A split-bus contingency row moves one end of a branch to a new bus section; branches the contingency moves off the same bus stay joined there | `scripts/check_prices.py` (`Outaged`); `out.network` does not apply these rows yet (NAM-09) | The contingency file gives the bus that stays as a number and a note for the one created | residual before and after applying the rows | verified |

## Not yet built (the checks that would catch everything above)

- The flow check: awards from the 60-day disclosure (NP3-966-ER) through the network
  should put every binding constraint at its limit and nothing enforced above it. It
  tests limits, the secured-flag reading (RAT-09) and the settlement point weights;
  it waits for the first disclosure that overlaps the archived models.
- The PSS/E parser has no cross-check against an independent reader.
