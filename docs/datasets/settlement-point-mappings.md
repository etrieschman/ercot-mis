# Settlement point mappings (NP4-160-SG, NP3-220-SG)

## What it is and why we use it

ERCOT's own statement of where each settlement point sits, in the vocabulary of its
price files: electrical buses. NP4-160-SG maps every electrical bus to its node name,
PSS/E name and number, voltage level, substation, load zone, resource node and hub;
NP3-220-SG lists the electrical buses that make up each hub and their kV. The DAM
model package carries the same facts in its own files (`Sp`, `Hb`, `Ld`); these two
products are the public, document-level version, and the only bridge from the LMP
file's bus names to the model.

## Source and capture

EWS, public classification, posted roughly twice a month, small. Pulled since the
start; parsed since 2026-10-06.

## Package layout

Zips of CSVs. NP4-160-SG: the full settlement point list with electrical bus mapping,
the resource node to unit list, the NOIE load mapping, the combined-cycle resource
names, and the hub and DC tie names. NP3-220-SG: the electrical bus to hub list and
the full electrical bus list. No operating date; the listing's posting time dates a
document.

## What we parse

Every member, by header (`raw/mappings.py`): `sp_electrical_bus_mapping`,
`sp_resource_node_units`, `sp_noie_mapping`, `sp_ccp_resource_names`,
`sp_hub_and_dc_tie_names`, `hub_buses`, `electrical_buses`.

## Decisions

- ERCOT's misspelt header for the logical resource node column is kept as the raw
  column `logical_resource_node_name`.
- Not yet used by core: the DAM package's own `Hb` and `Sp` files remain the source of
  settlement point weights (SP-01, SP-05). These tables are for comparing against them
  and for pricing nodes from the LMP file.

## ERCOT quirks

- The mapping's PSS/E bus numbers are the Network Operations Model's, not the DAM's
  hourly renumbering, so they do not join to a DAM hour by number.

## Validation

None beyond the header check.

## Open questions

- Electrical bus to DAM node: by name and voltage within a substation, to be measured.
- The hub list against the DAM `Hb` file per hour (hub buses present, energized).
