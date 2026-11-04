# Settlement point mappings (NP4-160-SG, NP3-220-SG)

## What it is and why we use it

ERCOT's own statement of where each settlement point sits, in the vocabulary of its
price files: electrical buses. NP4-160-SG maps every electrical bus to its RAW name,
PSS/E name and number, voltage level, substation, load zone, resource node and hub.
NP3-220-SG lists the electrical buses of each hub with their kV. The DAM package carries
the same facts in its `Sp`, `Hb` and `Ld` files; these two products are the public
version, and the only bridge from the LMP file's bus names to the model.

## Source and capture

EWS, public classification, posted about twice a month.

## Package layout

Zips of CSVs. NP4-160-SG: the settlement point list with its electrical bus mapping,
the resource node to unit list, the NOIE load mapping, the combined-cycle resource
names, and the hub and DC tie names. NP3-220-SG: the electrical bus to hub list and the
electrical bus list. A document has no operating date; the listing's posting time dates it.

## What we parse

Every member, by header (`raw/mappings.py`): `sp_electrical_bus_mapping`,
`sp_resource_node_units`, `sp_noie_mapping`, `sp_ccp_resource_names`,
`sp_hub_and_dc_tie_names`, `hub_buses` and `electrical_buses`. The raw rows carry no
operating date or hour.

## Decisions

- ERCOT's misspelt header for the logical resource node column is kept as the raw column
  `logical_resource_node_name`.
- Core does not read these tables. The DAM package's own `Hb` and `Sp` files are the
  source of settlement point weights (SP-01, SP-05). These tables are for comparing
  against them and for pricing the LMP file's buses.

## ERCOT quirks

- The mapping's PSS/E bus numbers are the Network Operations Model's, not the DAM's
  hourly numbering, so they do not join a DAM hour by number.

## Validation

The header check only.

## Open questions

- Electrical bus to DAM bus: by name and voltage within a substation. Unmeasured.
- The hub list against the DAM `Hb` file per hour. Unmeasured.
