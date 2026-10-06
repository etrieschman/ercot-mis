"""Run the parsers over archived CRR and DAM packages and check the results.

Reads the archive only and writes nothing. Prints counts and check results, never
values, so the output is safe to share.

Checks:
  * every member ERCOT ships is recognized (no "unknown" kinds);
  * PSS/E record counts match the data lines in each section (four per transformer);
  * DAM: RAW branches, transformers, loads and generators match the hour's CSVs;
  * GTL: every workbook parses, each delivery day has one row per hour and market
    for every GTC, and the day-ahead and real-time columns pair up.

    uv run python scripts/validate_parsers.py              # every CRR and GTL document, latest 2 DAM days
    uv run python scripts/validate_parsers.py --dam-days 32
"""

import argparse
import collections
import time
import zipfile

import polars as pl

import ercot_mis as em
from ercot_mis.raw import ParseError, crr, dam, gtl, psse


def packages(mis, emil_id, limit=None):
    rows = mis.query("""SELECT b.path FROM archive_blob b
           JOIN archive_source s USING (sha256)
           LEFT JOIN remote_doc d ON d.emil_id = s.emil_id AND d.doc_id = s.doc_id
           WHERE b.emil_id = ? GROUP BY b.path ORDER BY max(d.posted_at) DESC NULLS LAST""", [emil_id])
    return [mis.data_dir / path for (path,) in rows[:limit]]


def check_raw(data, label, problems):
    case = psse.parse_raw(data, label)
    for section, lines in case.line_counts.items():
        table = case.tables.get(f"psse_{section}")
        if table is None:
            continue
        expected = lines // 4 if section == "transformer" else lines
        if table.num_rows != expected or (section == "transformer" and lines % 4):
            problems.append(f"{label}: {section} has {table.num_rows} records from {lines} lines")
    return case


def validate_crr(mis, emil_id, problems):
    kinds, rows, dialects = collections.Counter(), collections.Counter(), collections.Counter()
    started = time.perf_counter()
    for path in packages(mis, emil_id):
        with zipfile.ZipFile(path) as package:
            for name in package.namelist():
                member = crr.classify_member(name)
                if member is None:
                    continue
                kinds[(member.kind, member.format, "parsed" if member.is_parsed else "archived")] += 1
                if not member.is_parsed:
                    continue
                label = f"{emil_id} {member.month} {member.kind}.{member.format}"
                try:
                    if member.kind == "network_model":
                        case = check_raw(package.read(name), label, problems)
                        dialects[case.dialect] += 1
                        tables = case.tables
                    else:
                        tables = crr.parse_member(member, package.read(name))
                except ParseError as error:
                    problems.append(str(error))
                    continue
                for table_name, table in tables.items():
                    rows[table_name] += table.num_rows
    print(f"\n{emil_id}: {len(packages(mis, emil_id))} packages in {time.perf_counter() - started:.1f}s, RAW dialects {dict(dialects)}")
    for (kind, fmt, status), count in sorted(kinds.items()):
        print(f"  {count:>4} {status:<8} {kind}.{fmt}")
    for table_name, count in sorted(rows.items()):
        print(f"  rows {table_name:<40} {count:>10,}")


def validate_dam(mis, days, problems):
    kinds, rows = collections.Counter(), collections.Counter()
    started, hours = time.perf_counter(), 0
    for path in packages(mis, "NP4-500-SG", days):
        per_hour = collections.defaultdict(dict)
        with zipfile.ZipFile(path) as package:
            for name in package.namelist():
                member = dam.classify_member(name)
                if member is None:
                    continue
                kinds[(member.kind, member.format, "parsed" if member.is_parsed else "archived")] += 1
                if not member.is_parsed:
                    continue
                label = f"DAM {member.operating_date} hour {member.hour} {member.kind}"
                try:
                    if member.kind == "network_model":
                        tables = check_raw(package.read(name), label, problems).tables
                        hours += 1
                    else:
                        tables = dam.parse_member(member, package.read(name))
                except ParseError as error:
                    problems.append(str(error))
                    continue
                for table_name, table in tables.items():
                    rows[table_name] += table.num_rows
                    per_hour[member.hour][table_name] = table.num_rows
        for hour, counts in per_hour.items():
            if hour is None:
                continue
            for raw_table, csv_table in (("psse_branch", "dam_lines"), ("psse_transformer", "dam_transformers"),
                                         ("psse_load", "dam_loads"), ("psse_generator", "dam_generators")):
                if counts.get(raw_table) != counts.get(csv_table):
                    problems.append(f"DAM {path.stem[:8]} hour {hour}: {raw_table}={counts.get(raw_table)} but {csv_table}={counts.get(csv_table)}")
    print(f"\nNP4-500-SG: {days} days, {hours} hourly models in {time.perf_counter() - started:.1f}s")
    for (kind, fmt, status), count in sorted(kinds.items()):
        print(f"  {count:>4} {status:<8} {kind}.{fmt}")
    for table_name, count in sorted(rows.items()):
        print(f"  rows {table_name:<40} {count:>10,}")


def validate_gtl(mis, problems):
    """NP3-766-M: one document per posting, not a zip; two shapes arrive under one format label."""
    shapes, days, rows = collections.Counter(), set(), 0
    started = time.perf_counter()
    paths = packages(mis, "NP3-766-M")
    for path in paths:
        data = path.read_bytes()
        member = gtl.classify_document(path.name, data)
        shapes[(member.kind, member.format)] += 1
        if not member.is_parsed:
            continue
        try:
            table = gtl.parse_gtl(data, f"GTL document {len(days) + 1}")
        except ParseError as error:
            problems.append(str(error))
            continue
        frame = pl.from_arrow(table)
        rows += frame.height
        for day, part in frame.group_by("delivery_date"):
            days.add(day[0])
            hours = part["hour_ending"].n_unique()
            if hours not in (23, 24, 25):
                problems.append(f"GTL {day[0]}: {hours} hours")
            per_gtc = part.group_by("gtc_name", "market").len()
            if per_gtc["len"].n_unique() != 1 or per_gtc.height % 2:
                problems.append(f"GTL {day[0]}: GTCs do not all have both markets for every hour")
    print(f"\nNP3-766-M: {len(paths)} documents, {len(days)} delivery days in {time.perf_counter() - started:.1f}s")
    for (kind, fmt), count in sorted(shapes.items()):
        print(f"  {count:>4} {'parsed' if kind == 'gtl_hourly' else 'archived':<8} {kind}.{fmt}")
    print(f"  rows {'gtl_hourly':<40} {rows:>10,}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dam-days", type=int, default=2)
    args = parser.parse_args()
    problems = []
    with em.open() as mis:
        validate_crr(mis, "NP7-800-M", problems)
        validate_crr(mis, "NP7-801-M", problems)
        validate_dam(mis, args.dam_days, problems)
        validate_gtl(mis, problems)
    print(f"\n{len(problems)} problems")
    for problem, count in collections.Counter(problems).most_common(30):
        print(f" - ({count}x) {problem[:300]}")
    raise SystemExit(1 if problems else 0)


if __name__ == "__main__":
    main()
