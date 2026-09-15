# ercot-mis

Fetch, cache and standardize ERCOT Market Information System (MIS) data locally,
with every table traceable to the bytes ERCOT published.

**Status: pre-alpha.** EWS listing, fetching and the content-addressed archive work;
the Public API client, parsers and data model are being built (see
[CLAUDE.md](CLAUDE.md) for the design and milestones).

## What it is for

- Pulling ERCOT products over **EWS** (certificate-authenticated; the only route to
  Secure and ECEII products such as the CRR and DAM network models) and the
  **Public API**.
- Keeping the original documents immutable and content-addressed, so every
  derived table records exactly which ERCOT file and which code produced it.
- Standardizing CRR and DAM network models (buses, branches, ratings,
  contingencies, generic transmission constraints) and DAM prices and cleared
  quantities into tidy Parquet tables you query from Python.

Everything is driven from Python:

```python
import ercot_mis as em

with em.open() as mis:                               # ./data in this repo, or $ERCOT_MIS_DATA
    docs = mis.list("NP7-800-M")                     # what EWS offers, with archive status
    result = mis.fetch("NP7-800-M", max_gb=1)        # download what isn't archived yet
    mis.fetch("NP4-500-SG", operating_dates=["2026-09-14"])
    mis.ingest("~/Downloads/ercot")                  # adopt zips downloaded elsewhere
```

Documents are stored once, by content hash, under `data/archive/`, and never edited.
`data/catalog.duckdb` records every document ERCOT listed, every archived file and
zip member, and how each arrived.

## Confidentiality

ercot-mis is public code for data that often is not public. Secure and ECEII data
must stay on your machine:

- all data lives in `data/` (gitignored) or wherever `ERCOT_MIS_DATA` points;
  `em.open()` warns if that folder is inside a cloud-synced location;
- your DUNS and API user ID come from the environment or a gitignored `.env`;
- a pre-commit check (`tools/check_confidential.py`) blocks DUNS-like numbers, API
  user IDs, certificates, PSS/E `.RAW` files and anything named ECEII.

Before storing ECEII data in any cloud service, check your ERCOT ECEII obligations.

## Setup

```bash
uv sync
git config core.hooksPath .githooks
cp .env.example .env    # then fill in ERCOT_DUNS and ERCOT_API_USER
```

### Credentials

EWS requires an ERCOT **API** certificate (user ID prefixed `API_`), not the
personal certificate used to log into MIS in a browser. A USA creates one in MPIM
with the "API Certificate" box ticked. Convert the `.pfx` into the PEM pair Python
needs (`-legacy` because ERCOT wraps the bundle with RC2-40-CBC):

```bash
mkdir -p ~/.ercot
openssl pkcs12 -legacy -in API_CERT.pfx -clcerts -nokeys -out ~/.ercot/api.crt
openssl pkcs12 -legacy -in API_CERT.pfx -nocerts -nodes -out ~/.ercot/api.key
chmod 600 ~/.ercot/api.key
```

`api.key` is an unencrypted private key: keep it outside any repository or synced
folder. If `xmlsec` fails to build, install the C library with
`brew install libxmlsec1 pkg-config`.

## Keep a daily archive

EWS keeps nothing older than each product's display window: 31 days for DAM network
models, one year for CRR network models. Anything not captured before then is gone,
so run the daily pull every day:

```bash
uv run python examples/daily_pull.py
```

`examples/launchd/` has a launchd template that runs it every morning on macOS.
`examples/probe.py` lists what EWS offers for each product without downloading.

## Tests

```bash
uv run pytest -q
```

## License

MIT
