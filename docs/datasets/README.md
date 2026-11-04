# Dataset notes

One note per dataset: what ERCOT publishes, what the repo parses from it, each import
decision with its reason, and the quirks the core layer handles. Code docstrings say
what a function returns; these notes say what the files are.

Notes are public. They describe layouts, column names and formats, never an equipment,
bus, substation, contingency or constraint name, and never a document file name.

Notes hold no measurements. Counts, rates and the dates of runs live in the scripts that
measure them (`scripts/validate_parsers.py`, `scripts/check.py`) and in the reports they
write under `data/reports/`.

| note | products | state |
|---|---|---|
| [psse-raw.md](psse-raw.md) | PSS/E v30 RAW, inside the CRR and DAM packages | parsed; validated by `scripts/validate_parsers.py` |
| [crr-network-model.md](crr-network-model.md) | NP7-801-M (annual), NP7-800-M (monthly) | parsed; validated |
| [dam-network-model.md](dam-network-model.md) | NP4-500-SG | parsed; validated |
| [identity-and-matching.md](identity-and-matching.md) | keys across CRR and DAM | measured by `checks/identity.py` and `checks/register.py` |
| [generic-transmission-limits.md](generic-transmission-limits.md) | NP3-766-M, NP3-770-M | limits parsed; definitions archived |
| [settlement-point-mappings.md](settlement-point-mappings.md) | NP4-160-SG, NP3-220-SG | parsed; not read by core |
| [dam-prices.md](dam-prices.md) | NP4-191-CD, NP4-183-CD, NP4-190-CD, NP4-523-CD, NP4-200-CD, NP4-158-SG, NP4-231-CD, NP4-159-CD | parsed; tested by `checks/prices.py` |
| [dam-disclosure.md](dam-disclosure.md) | NP3-966-ER | parsed; tested by `checks/flows.py` |
| [planning-inputs.md](planning-inputs.md) | outages, offers, load, wind and solar | listed in `products.py`, not fetched |
| not written | NP5-615-SG Standard Contingency List | archived, not parsed |

## Template

```markdown
# <Dataset> (<EMIL IDs>)

## What it is and why we use it
## Source and capture        channel, report type, display window, cadence
## Package layout            members and naming, in masked form
## What we parse             member -> raw table; what is archived but not parsed, and why
## Decisions                 each choice with its reason and its register row
## ERCOT quirks              what the core layer must handle
## Validation                what scripts/validate_parsers.py and the checks measure
## Open questions            what is unknown, and which document would settle it
```
