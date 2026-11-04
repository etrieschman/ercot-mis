# ercot-mis

ercot-mis fetches ERCOT Market Information System (MIS) data, keeps the original files,
and standardizes them into tables that record which ERCOT file and which code produced
every row. A daily pull archives the CRR and DAM network models, the generic transmission
limits, ERCOT's settlement point mappings, the day-ahead prices and pricing inputs and the
60-day disclosure, builds the raw and core tables, and checks the DAM's prices and flows and the CRR auction's prices
against what ERCOT published. `CLAUDE.md` holds the design and the open work;
`demo/tour.ipynb` walks through every call.

## What it does

- Fetches ERCOT products over EWS (certificate-authenticated, the only route to Secure
  and ECEII products such as the network models) and over the Public API.
- Keeps every original document immutable and content-addressed, so every derived table
  records which ERCOT file and which code produced it.
- Standardizes the CRR and DAM network models (buses, CRR nodes, branches, ratings,
  contingencies, generic transmission constraints), the DAM's prices and its cleared
  awards into Parquet tables read from Python.

Everything runs from Python:

```python
import ercot_mis as em

with em.open() as session:                           # ./data in this repo, or $ERCOT_MIS_DATA
    docs = session.list("NP7-800-M")                 # what EWS offers, with archive status
    result = session.fetch("NP7-800-M", max_gb=1)    # download what is not archived yet
    session.fetch("NP4-500-SG", operating_dates=["2026-09-14"])
    session.ingest("~/Downloads/ercot")              # adopt zips downloaded elsewhere
```

Documents are stored once, by content hash, under `data/archive/`, and never edited.
`data/catalog.duckdb` records every document ERCOT listed, every archived file and zip
member, how each arrived, and every table built from them.

```python
with em.open() as session:
    session.build_raw("NP4-500-SG")                  # parsed rows, one Parquet per package and table
    session.build_core()                             # snapshots, buses, nodes, branches, ratings, contingencies, GTCs
    raw_buses = session.raw("psse_bus").filter(pl.col("operating_date") == "2026-09-15").collect()
    buses = session.core("bus").filter(pl.col("snapshot_id") == "dam:2026-09-15:he12:r1").collect()
    net = session.network("dam:2026-09-15:he12:r1")  # one snapshot as a DC model reads it
    branches = session.match_branches("crr:monthly:2026-09:r1", "dam:2026-09-15:he12:r1")
```

Three words are used strictly (`docs/datasets/identity-and-matching.md`). A **node** is a
point of a node-breaker model: one record of the CRR RAW. A **bus** is a point of a
bus-branch model: one record of the DAM RAW, or the CRR nodes that closed breakers join
into one electrical point. A **substation** is the yard holding buses at several voltages.
PSS/E calls every record a bus; only the raw layer keeps that word.

```python
with em.open() as session:
    page = session.viewer("crr:monthly:2026-09:r1", "dam:2026-09-15:he12:r1")   # one HTML file, substation by substation
```

The viewer page holds CEII (equipment names). It is written inside the data folder and
must not be shared.

The checks in `ercot_mis/checks/` test the layers against what ERCOT published. Each
writes a report under `data/reports/<check>/`:

```bash
uv run python scripts/check.py prices --day 2026-09-30
```

`prices` rebuilds every DAM settlement point price from the binding constraints; `flows` pushes
the DAM's own awards through the network; `crr_prices` rebuilds the CRR auction's settlement
point shadow prices on the monthly CRR model; `register` measures every row of
`docs/assumptions.md` that no other check covers. The script's docstring lists the rest.

`docs/out-network.md` states the conventions behind `network()`; `docs/assumptions.md`
lists every choice the code makes and how it was checked; `docs/datasets/` has one note
per ERCOT dataset.

Builds run from a script or a notebook: the process pool needs an importable `__main__`.

## Confidentiality

ercot-mis is public code for data that often is not public. Secure and ECEII data stay on
your machine:

- all data lives in `data/` (gitignored) or wherever `ERCOT_MIS_DATA` points; `em.open()`
  warns if that folder is inside a cloud-synced location;
- your DUNS and API user id come from the environment or a gitignored `.env`;
- a pre-commit check (`tools/check_confidential.py`) blocks DUNS-like numbers, API user
  ids, certificates, PSS/E `.RAW` files and anything named ECEII.

Before storing ECEII data in any cloud service, check your ERCOT ECEII obligations.

## Setup

```bash
uv sync
git config core.hooksPath .githooks
cp .env.example .env    # then fill in ERCOT_DUNS and ERCOT_API_USER
```

### Credentials

EWS requires an ERCOT **API** certificate (user id prefixed `API_`), not the personal
certificate used to log into MIS in a browser. A USA creates one in MPIM with the "API
Certificate" box ticked. Convert the `.pfx` into the PEM pair Python needs (`-legacy`
because ERCOT wraps the bundle with RC2-40-CBC):

```bash
mkdir -p ~/.ercot
openssl pkcs12 -legacy -in API_CERT.pfx -clcerts -nokeys -out ~/.ercot/api.crt
openssl pkcs12 -legacy -in API_CERT.pfx -nocerts -nodes -out ~/.ercot/api.key
chmod 600 ~/.ercot/api.key
```

`api.key` is an unencrypted private key: keep it outside any repository or synced folder.
If `xmlsec` fails to build, install the C library with `brew install libxmlsec1 pkg-config`.

Public API credentials belong in the macOS Keychain rather than `.env`; ercot-mis reads
them from there when the variable is unset:

```bash
security add-generic-password -s ercot-mis -a ERCOT_PUBLIC_API_USERNAME -w
security add-generic-password -s ercot-mis -a ERCOT_PUBLIC_API_PASSWORD -w
security add-generic-password -s ercot-mis -a ERCOT_PUBLIC_API_SUBSCRIPTION_KEY -w
```

Run each command exactly as written, with nothing after `-w`: it prompts
`password data for new item:` and you paste the value (the sign-in email, the password,
or the key) blind, twice. Check with
`security find-generic-password -s ercot-mis -a ERCOT_PUBLIC_API_PASSWORD -w`.

### Keeping the data local

`em.open()` makes the data folder and everything directly inside it owner-only. Backups
copy ECEII too: exclude the folder from Time Machine unless the backup disk is local and
encrypted:

```bash
tmutil addexclusion data
```

## Keep a daily archive

EWS keeps nothing older than each product's display window: 31 days for DAM network
models, one year for CRR network models. Anything not captured before then is gone, so
run the daily pull every day:

```bash
uv run python scripts/daily_pull.py
```

`scripts/launchd/` has a launchd template that runs it every morning on macOS. Transient
download errors are retried within the run. The run does not keep the Mac awake; a missed
pull (no run for a day and a half, a run that slept, a failure) is flagged with a quiet
macOS notification, and `data/logs/last_run.json` records the flags and the days until
EWS data could first be lost. When that falls within a week the notification turns urgent
and its text is the command to paste, which fetches only the EWS products:

```bash
uv run python scripts/urgent_pull.py
```

Notifications stay in Notification Center; to keep them on screen until dismissed, set
Script Editor's notification style to Persistent in System Settings. `scripts/probe.py`
lists what EWS offers for each product without downloading.

## Move to a new machine

A clone carries only code. On the new machine:

1. Install [uv](https://docs.astral.sh/uv/) and Homebrew's `libxmlsec1 pkg-config`, clone
   the repository, then run the commands under [Setup](#setup).
2. Copy the credentials from the old machine by AirDrop or a drive, never a synced folder:
   `~/.ercot/` (`api.crt`, `api.key`; then `chmod 700 ~/.ercot && chmod 600
   ~/.ercot/api.key`) and the repository's `.env`. Add the three Public API secrets to the
   new Keychain (see [Credentials](#credentials)); on the old machine
   `security find-generic-password -s ercot-mis -a <NAME> -w` shows each value.
3. Stop the old machine's pull, so the copy is complete:
   `launchctl unload ~/Library/LaunchAgents/ercot-mis.daily-pull.plist`.
4. Copy the data folder whole, from the old machine (Remote Login on the new one):

   ```bash
   rsync -a --progress data/ <newmac>.local:<path to repo>/data/
   ```

   Then `tmutil addexclusion data` on the new machine.
5. Run the pull once by hand, `uv run python scripts/daily_pull.py`, and check
   `data/logs/last_run.json`.
6. Install the schedule:

   ```bash
   sed -e "s#REPO#$PWD#g" -e "s#<string>UV</string>#<string>$(which uv)</string>#" \
       scripts/launchd/ercot-mis.daily-pull.plist > ~/Library/LaunchAgents/ercot-mis.daily-pull.plist
   launchctl load ~/Library/LaunchAgents/ercot-mis.daily-pull.plist
   ```

Run the pull on one machine only: two pulls make two archives that drift apart.

## Tests

```bash
uv run pytest -q
```

## License

MIT
