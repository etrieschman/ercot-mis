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
| TOP-01 | option | CRR ties (breakers at the minimum reactance) stay as branches and monitored ones are enforced | `out/network.py` `contract_ties=False` | The auction solves the RAW as given; the monitored CSV lists breakers with real ratings | `scripts/check_network.py`: the uncontracted model factorizes and solves, tie flows are determinate, PTDF rows differ from the contracted ones by small amounts; the contracted model drops the monitored ties' limits | verified (contraction is opt-in) |
| TOP-02 | forced | Bus identity for matching uses the contracted key | `core/node.py` | n/a (our construct) | keys stable across DAM hours and CRR months (identity report) | verified |
| TOP-03 | stated | Out-of-service branches are not part of the model | `out/network.py` | DAM README: the RAW incorporates the outage scheduler; CRR outages that name a workbook operations name are all out of service in the RAW | DAM: the README says so. CRR: checked by hand on one month (2026-09, outages naming a workbook operations name were out of service in the RAW); no script re-measures it | verified (as reading; not re-measured) |
| TOP-04 | stated | The CRR Outages file is informational | `raw/crr.py` parses it; core does not apply it | Same as above: the RAW already reflects it | same one-month hand check as TOP-03; no script | open (as reading) |
| TOP-05 | forced | Isolated nodes and minor islands are dropped; the largest component is kept | `out/network.py` | The engines must do the same to solve | harmless; counts in `dropped_nodes` | verified |
| TOP-06 | stated | The slack is ERCOT's swing node (PSS/E's "swing bus") | `out/network.py` `slack_source` | DAM README names the MMS-DAM slack; the RAW type-3 record is it. CRR RAW marks one per island | measured; fallback recorded | verified |
| TOP-07 | forced | Ambiguous bus keys (identical attachments at one substation) get an ordinal suffix | `core/node.py` | n/a | not asserted: nothing checks that an ambiguous key stays out of `Network.nodes` | open (believed harmless; add the assertion) |
| TOP-08 | measured | DAM contingency files differ by hour, so each hour is its own snapshot | `core/snapshot.py` | ERCOT posts one per hour | `scripts/measure_identity.py` compares generator, load, settlement point and line name sets across the hours of a day; contingency names are not compared yet | verified (as reading; contingency names unmeasured) |
| TOP-09 | forced | Ties out of service are not contracted even when contraction is on | `core/node.py` | An open breaker separates nodes | by construction | verified |
| TOP-11 | forced | Packages that describe one logical model (a DAM day, a CRR month, an annual term and sequence; `_Upd` included) are revisions numbered by posting time, an unknown posting time counting as oldest; checks, the viewer and the price check take the highest revision number | `core/snapshot.py`; `scripts/check_prices.py` (`hour_ids`, `borrowed_gtcs`); `scripts/check_network.py` | ERCOT posts an `_Upd` as a new document and says nothing about precedence | `tests/test_build.py` orders revisions by posting time; diff r1 against r2 once an `_Upd` month overlaps | open (ordering tested; precedence unverified) |
| TOP-10 | measured | A CRR tie is an in-service line (never a transformer) with reactance magnitude at or below `TIE_REACTANCE` (1e-4 pu); nothing else makes a tie | `core/node.py` (`TIE_REACTANCE`, tie groups), `core/branch.py` (`is_tie`) | PSS/E's minimum reactance in ERCOT's CRR export marks closed breakers and switches | `scripts/measure_identity.py`: branches at or below the threshold, same kV at both ends, zero resistance | verified |
| TOP-12 | forced | A CRR node's substation (viewer only) is its matched DAM node's; else spread over ties and transformers of any status; else along matched lines; else its own name, marked `~` | `viewer/__init__.py` `crr_substations` | The CRR RAW names nodes, not substations | `nodes_with_substation` in the viewer's coverage counts | open (viewer only; not a core fact) |

## Impedances and ratings

| id | type | assumption | where | ERCOT | check | status |
|---|---|---|---|---|---|---|
| RAT-01 | stated | Reactance and tap are used as written (`CW=1`, `CZ=1`, no phase shifters) | `core/branch.py` (tap = windv1/windv2, angle kept as `angle_deg`); `shift_factors.py` (susceptance 1/(x·tap), angle ignored) | PSS/E conventions | not asserted in code: a RAW with `ang1 != 0` or `CW != 1` would be modelled silently; add a guard in `core.branch` and a non-unit-tap test | open (PSS/E convention assumed; no guard) |
| RAT-02 | measured | DAM clamps reactance at a floor; comparisons raise both sides to it | `core/match.py` | DAM RAW has no branch below the floor | measured | verified |
| RAT-03 | measured | Negative reactances (series capacitors) are kept | `out/network.py` | Both RAWs carry them | present in both | verified |
| RAT-04 | forced | DAM base limit is rate A, post-contingency rate B; CRR base is BaseCaseRating, post is EmergencyRating | `out/network.py` | Not stated in the DAM README; ERCOT's NOMCR rating conventions (normal / emergency / 15-minute) | read the ERCOT rating conventions and compare rate B with the CRR emergency rating on matched branches | open |
| RAT-05 | forced | A base rating of zero or null means no limit, even when the emergency rating is positive; an emergency rating of zero or null falls back to the base rating as the post-contingency limit | `out/network.py` | PSS/E convention | counts of each case per snapshot (not yet in `core.coverage`) | open (two rules; PSS/E would read a zero emergency rating as no post-contingency limit; ERCOT's reading unknown) |
| RAT-06 | option | CRR limits come from the monitored CSV, one time-of-use block; the RAW is PeakWD | `out/network.py` `time_of_use` | The auction clears per block with the block's ratings | blocks identical this month | verified |
| RAT-07 | measured | DynamicRatings is not applied | `raw/crr.py` (archived) | The workbook holds monthly temperature assumptions, not element ratings; the monitored CSV already carries the resulting ratings | workbook shape inspected | verified |
| RAT-08 | option | Every limited branch is enforced under every contingency | `out/network.py` | Both engines screen and enforce a subset; the published DAM binding constraints are the ones that bound | this is the full model, not the enforced subset; ftr_align decides which it compares | verified (documented) |
| RAT-09 | measured | DAM enforces the secured set; monitored-only rows are unlimited | `out/network.py` `limits` | CIM `DAM Secured` flag reading, unverified | `scripts/check_prices.py` counts every binding branch row by the branch's (secured, monitored, limited) flags (`binding_rows_by_limit_flags`) | verified (2026-09-30, six hours: every binding branch row sits on a secured, limited branch; none on a monitored-only or unflagged one) |
| RAT-10 | forced | A contingency with no branch outage and no split-bus move is dropped as empty. In the price check a binding row under such a contingency is priced as base case only when the contingency had generator, load or settlement point rows alone; when its branches are ones our model lacks or has out of service, the row is a topology discrepancy: counted (`binding_under_contingency_absent_in_model`), not priced. A contingency applied with fewer branches than ERCOT names is priced and flagged (`used_contingency_partial`) | `out/network.py`; `scripts/check_prices.py` | ERCOT's engine removes every element of a definition; an element absent from our RAW is a difference between the models | by construction; `dropped_contingencies` carries the counts; the price report carries both outcomes | verified |
| RAT-11 | forced | DC branch susceptance is 1/(x·tap); the phase-shift angle is ignored | `shift_factors.py` `DcSystem.__init__` | PSS/E's DC power flow convention; some engines ignore the tap | the price check on transformer rows (implicit in PRC-01); a run with tap forced to one, as PRC-06 was done for the cutoff, not yet made | open |
| RAT-12 | forced | When a rating source lists a branch more than once for one block, the first row is used; the same for a CRR tag with two operations names, two contingencies normalizing to one name, and a GTC listed twice | `out/network.py` `_limits`, `core/diff.py`, `core/match.py`, `core/gtc.py` (`unique(keep="first")`) | n/a | duplicates per source counted nowhere yet | open (report the duplicates instead of resolving them silently) |

## Names, orientation, constraints

| id | type | assumption | where | ERCOT | check | status |
|---|---|---|---|---|---|---|
| NAM-01 | stated | CRR line names run from RAW `i` to `j` | `core/branch.py` | The comment is "i name j name ckt" | by hand on one month (2026-09); no script | verified (by hand) |
| NAM-02 | measured | CRR transformer names follow the `Autos` sheet, which is reversed relative to the RAW for a large minority; flow directions refer to the name | `core/branch.py` `is_name_reversed`, `out/network.py` GTC sign | The CSVs name transformers by the sheet | by hand on one month (2026-09): a large minority reversed; the sign flip is applied; no script re-measures the share | verified (by hand) |
| NAM-03 | stated | GTC member factors are signed From-To positive | `out/network.py` | CSV `FlowDirection` | by construction | verified |
| NAM-04 | forced | DAM GTC limits are the GTL workbook's DAM column for the delivery day, latest posting wins | `core/build.py` | The workbook is reposted; the latest posting is ERCOT's final | document IDs do not follow posting time, so the catalog's posting time is used | verified |
| NAM-05 | forced | DAM GTC members are not known; the CRR id comes from a manual crosswalk | `core/gtc.py` | NP3-770-M defines them; the DAM names a binding GTC by the CRR code | `scripts/check_prices.py` borrows the CRR members through `core.match_branch` and prices the binding GTCs with them; not yet in `out.network` | open (borrowing measured) |
| NAM-06 | forced | Contingency matching is one-to-one | `core/match.py` | CRR splits what DAM lumps in places | many unmatched CRR contingencies sit inside one DAM contingency | open (a `subset` method) |
| NAM-07 | forced | Settlement points a contingency disconnects are not special-cased | `raw/dam.py` parses `SpCtg`; core does not use it | The DAM knows which single-bus settlement points a contingency islands | table shape inspected; semantics from the README | open (the de-energized list NP4-200-CD is pulled; the price report counts how many of its points we price; the `SpCtg` file is still unused) |
| NAM-09 | forced | A split-bus contingency row moves one end of a branch to a new bus section; branches the contingency moves off the same bus stay joined there; split rows on loads, generators and settlement points are counted, not applied | `core/contingency.py` (`split_end`), `out/network.py` (`split_branch_indexes`, `split_ends`), `shift_factors.DcSystem.outaged` | They change topology under the contingency (PRC-05) | the price residual before and after applying the rows (`scripts/check_prices.py`, 2026-10-01); a contingency with only split rows is no longer dropped as empty | verified (applied in `out.network` since 2026-10-06) |
| NAM-08 | stated | Every hourly fact carries `interval_start_utc`, and hourly tables join on it: a DAM study hour counts from local midnight; a price row's (date, hour ending, DST flag) names the instant; the GTL workbook's second identical timestamp is the repeated hour | `clock.py`; `core/snapshot.py`, `raw/gtl.py`, `core/build.py` (GTL join), `scripts/check_prices.py` (price join) | DAM README: 23 or 25 hourly models, the extra hour third; price files flag the repeated hour with `DSTFlag` | `tests/test_clock.py` and `tests/test_gtl.py` on the 2026 transition days; the first real long day is 2026-11-01 | verified (by construction and synthetic tests; confirm on 2026-11-01) |

## Settlement points

| id | type | assumption | where | ERCOT | check | status |
|---|---|---|---|---|---|---|
| SP-01 | stated | A DAM hub weighs each Hub Bus equally and, inside a Hub Bus, each energized power flow bus equally; de-energized buses are left out; the hub average is the equal average of the North, South, Houston and West hubs and the bus average weighs every Hub Bus of those four equally | `core/settlement_point.py` | Protocols 3.5.2.1(3) and its siblings (hub price = system lambda minus the hub's aggregated shift factor times each shadow price, with per-Hub-Bus and per-bus distribution factors), 3.5.2.6 and 3.5.2.7 for the average hubs | the hub residual in `scripts/check_prices.py` before and after (2026-10-06: no hub was within a cent under the flat average) | verified (as reading; measured in the price reports) |
| SP-02 | stated | Load zone weights are the loads' MW distribution factors, in-service loads only | `core/settlement_point.py` | Protocols 4.6.1.2: the zone price uses each energized power flow bus's share of the zone's load for the constraint; the `Ld` file carries the DAM's own load shares. NP4-159-CD publishes ERCOT's load distribution factors (pulled since 2026-10-06, not yet compared) | consistent with the `Ld` file | verified (as reading); comparison with NP4-159-CD open |
| SP-03 | measured | CRR hub and zone weights are normalized to one | `core/settlement_point.py` | The CSV gives MW-scale weights | measured | verified |
| SP-05 | stated | Logical resource nodes take their combined-cycle point's bus | `core/settlement_point.py` | The `Sp` file names it | rows without one stay unresolved | verified |
| SP-06 | forced | Without contraction a CRR settlement point's weight is spread equally over the nodes of its bus; the node the SourcesAndSinks file names is not singled out | `out/network.py` (settlement points) | The CRR file places each weight on one node | node-level shift factor at the file's node against the spread, on monitored ties (`scripts/check_network.py`), not yet made | open |
| SP-07 | forced | CRR settlement point kinds come from name prefixes (`HB_`, `LZ_`, `DC`); a DAM DC-tie point without an `Sp` bus takes its zone's in-service loads by distribution factor | `core/settlement_point.py` | The CRR file gives no type; the DAM `Ld` file carries DC-tie zone names | `diff_settlement_point.same_kind`; count of DC-tie points resolved through loads | open |

## Matching

| id | type | assumption | where | ERCOT | check | status |
|---|---|---|---|---|---|---|
| MAT-01 | forced | A CRR branch is its DAM branch when the workbook's operations name equals the DAM name once punctuation and case are ignored (`exact`), or followed by the CRR circuit id (`ops+ckt`) | `core/match.py` | No shared key is published; the workbook gives the operations name, DAM appends its own circuit designator | `scripts/measure_identity.py`: rows per method; kind, voltage and reactance of matched pairs | verified (agreement measured; no independent key exists) |
| MAT-02 | forced | Otherwise the one DAM name that is the operations name plus at most two characters (`prefix`); among several, the one with the same reactance (`prefix+x`) | `core/match.py` | Same as above | `scripts/measure_identity.py`: reactance agreement by method | verified (agreement measured; no independent key exists) |
| MAT-03 | forced | Buses match through a settlement point both models place on one bus (`settlement_point`), then by the vote of matched branch endpoints (`branch_endpoints`); never by node number or name | `core/match.py` | CRR and DAM bus numbers are unrelated and DAM renumbers every hour | `scripts/measure_identity.py`: rows per method; voltage of matched nodes | verified (agreement measured; no independent key exists) |
| MAT-04 | forced | Contingencies match by name (`name`), then by an identical set of translated branches (`members`) | `core/match.py` | Names are shared for most; member sets differ and the difference is recorded | `scripts/measure_identity.py`: rows per method; why name-matched member sets differ | verified (agreement measured; no independent key exists) |
| MAT-05 | forced | Anything no method settles stays `unmatched` and is output; nothing is guessed | `core/match.py` | n/a | by construction; unmatched rows counted in the identity report | verified |
| MAT-06 | forced | The one `Operations_Name` shared by more than five workbook rows is the placeholder for unmapped rows and is ignored | `core/match.py` `placeholder_name` | The workbook marks unmapped rows with one placeholder text | the counts behind the most and second most common names (not yet in `measure_identity.py`) | open |
| MAT-07 | forced | Two reactances agree when, both raised to the DAM floor, they differ by at most one percent of the larger; this decides `prefix+x` matches and `same_reactance` verdicts | `core/match.py` `reactance_agrees`, `core/diff.py` | n/a | agreement rates by match method in `scripts/measure_identity.py`; the tolerance itself is not justified by a distribution | open |

## Prices (the external test)

| id | type | assumption | where | ERCOT | check | status |
|---|---|---|---|---|---|---|
| PRC-01 | measured | A DAM price is the system price minus shadow price times shift factor, summed over the binding rows, with the shift factor in the branch's from-to orientation and the row's direction sign; no loss term | `scripts/check_prices.py` | ERCOT prices carry no marginal loss component | the residual at every settlement point, per DAM hour, under this convention; the opposite sign is kept as a diagnostic (`residual_if_sign_flipped`, worse in every hour measured); the headline counts only when `share_of_shadow_price_used` is one | verified (hours without a binding GTC reproduce to a small residual; hours with one are PRC-04) |
| PRC-02 | measured | A binding row's direction is its from and to substation; inside one substation, the two voltages to the tenth of a kV | `scripts/check_prices.py` | The published flow is unsigned; the tenths digit of base kV tells bus sections apart | every binding branch row resolves to a direction; reading the voltage to the whole kV put one line the wrong way round and showed as the largest residual | verified |
| PRC-03 | stated | Under a contingency that cuts buses off, a hub's or zone's weights are renormalized over the buses still energized for that constraint; a point with no bus left has no sensitivity to that constraint | `scripts/check_prices.py` (`aggregated`) | Protocols 4.6.1.2 and 3.5.2 (distribution factors are per constraint, over energized buses). The same-substation average of 4.5.1(8)(b) is for buses de-energized in the base case, which the network drops before pricing (NP4-200-CD lists them; the mapping of 4.5.1(8)(a) is NP4-231-CD, not applied yet) | the cut-off points' residual under this rule and under the plain reading (cut-off buses at zero weight, no renormalization), both in every price report; the same-substation average tried on islanded points made the worst of them several dollars worse (2026-09-30) | verified (as reading; measured on 2026-09-30) |
| PRC-04 | forced | A binding GTC is priced with the CRR model's members for the same month, translated to DAM branches | `scripts/check_prices.py` | The DAM's own definition is published only as documents: NP3-770-M is a zip of one PDF per GTC (text, six to nine pages, with the member lines), plus slides | across the hours a GTC binds, the residual is one fixed pattern times its shadow price; regressing that pattern on nearby shift factors (2026-10-01) pointed at two parallel lines the CRR's definition lacks. That inference is a diagnostic, not a source (2026-10-06): the hand-kept member file is no longer read | open (external: parse the NP3-770-M PDFs; until then the evening residual stands) |
| PRC-05 | measured | A split-bus contingency row moves one end of a branch to a new bus section; branches the contingency moves off the same bus stay joined there | `out/network.py` builds the moves, `shift_factors.DcSystem.outaged` applies them (NAM-09) | The contingency file gives the bus that stays as a number and a note for the one created | residual before and after applying the rows | verified |
| PRC-06 | measured | Shift factors enter the price at full precision: no cutoff below which a node's sensitivity is treated as zero | `scripts/check_prices.py`, `shift_factors.py` | ERCOT's EMS drops shift factors below 0.0001 before they reach the market systems (Business Practice Manual); the two-percent rule applies to deciding whether a constraint is resolvable, not to pricing | 2026-09-30 hours 12 and 19 re-run with a 0.0001 cutoff (residual unchanged) and a 0.02 cutoff (residual several times worse) | verified |

## Not yet built (the checks that would catch everything above)

- The flow check: awards from the 60-day disclosure (NP3-966-ER) through the network
  should put every binding constraint at its limit and nothing enforced above it. It
  tests limits, the secured-flag reading (RAT-09) and the settlement point weights;
  it waits for the first disclosure that overlaps the archived models.
- The PSS/E parser has no cross-check against an independent reader.
