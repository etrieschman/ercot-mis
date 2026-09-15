# Dataset notes

One note per dataset: the design choices behind importing it, and why. Code
docstrings say *how* a parser works; these notes record what ERCOT actually ships,
what we decided, and what was verified against real files.

**Structure only.** Notes are public. Describe layouts, column names, counts and
formats; never bus, station, device, contingency or constraint names, or file names.

| note | products | status |
|---|---|---|
| [psse-raw.md](psse-raw.md) | PSS/E v30 RAW, shared by CRR and DAM | parsed, validated |
| [crr-network-model.md](crr-network-model.md) | NP7-801-M (annual), NP7-800-M (monthly) | parsed, validated |
| [dam-network-model.md](dam-network-model.md) | NP4-500-SG | parsed, validated |
| _to write_ | NP4-160-SG, NP3-220-SG, NP5-615-SG | archived, not parsed |
| _to write_ | NP4-190-CD, NP4-191-CD, NP4-183-CD, NP3-966-ER, NP4-159-CD | Public API client not built |

## Template

```markdown
# <Dataset> (<EMIL IDs>)

## What it is and why we use it
## Source and capture        channel, report type, display window, cadence, size
## Package layout            members and naming, in masked form
## What we parse             member -> raw table; what is archived but not parsed, and why
## Decisions                 each choice with its reason (e.g. CSV over XML)
## ERCOT quirks              surprises the core layer must handle
## Validation                what scripts/validate_parsers.py checks, and the last clean run
## Open questions
```
