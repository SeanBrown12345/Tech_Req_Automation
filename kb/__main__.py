"""Command line: python -m kb {ingest,stats,search,export}."""

import argparse
import json
import sys
from pathlib import Path

from kb import store
from kb.extract import extract_profile
from kb.profile import load_profile, load_scales

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
DEFAULT_DB = ROOT / "data" / "kb.sqlite"


def cmd_ingest(args, conn):
    scales = load_scales(CONFIG / "scales.yaml")
    paths = [Path(p) for p in args.profiles] or sorted((CONFIG / "profiles").glob("*.yaml"))
    failed = False
    for path in paths:
        try:
            profile = load_profile(path, scales, ROOT)
            records, reports = extract_profile(profile)
        except Exception as exc:  # report and keep loading the other workbooks
            print(f"\n[FAIL] {path.name}: {exc}")
            failed = True
            continue
        store.replace_source(conn, profile, records)
        print(f"\n[OK] {profile.source_id}: {len(records)} records from {profile.file.name}")
        for rep in reports:
            dist = ", ".join(f"{s or 'NONE'}={n}" for s, n in rep.statuses.most_common())
            print(f"  {rep.sheet:<28} {rep.records:>5} records  {rep.skipped_unanswered:>4} unanswered skipped  [{dist}]")
            for (signal, value), n in rep.unknown_values.most_common():
                print(f"    ! unmapped {signal} value {value!r} x{n} -> add it to config/scales.yaml")
            if rep.multi_marked:
                print(f"    ! {rep.multi_marked} rows had more than one answer column marked (first one used)")
    return 1 if failed else 0


def cmd_stats(args, conn):
    for row in conn.execute(
            "SELECT s.source_id, s.client, s.submitted, COUNT(r.record_id) n FROM sources s"
            " LEFT JOIN records r USING (source_id) GROUP BY s.source_id ORDER BY s.source_id"):
        print(f"{row['source_id']:<28} {row['client'] or '':<26} submitted={row['submitted'] or '?':<10} {row['n']:>6}")
    print()
    for row in conn.execute("SELECT status, COUNT(*) n FROM records GROUP BY status ORDER BY n DESC"):
        print(f"  {row['status'] or 'NONE':<18} {row['n']:>6}")
    total = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    print(f"  {'TOTAL':<18} {total:>6}")
    return 0


def cmd_search(args, conn):
    for r in store.search(conn, args.query, args.limit, args.status):
        print(f"\n[{r['status'] or '?'}] {r['client']} / {r['sheet']} {r['req_id'] or ''}  (raw: {r['answer_raw']})")
        if r["parent_text"]:
            print(f"  under: {r['parent_text']}")
        print(f"  REQ: {r['requirement']}")
        if r["comment"]:
            print(f"  ANS: {r['comment']}")
    return 0


def cmd_export(args, conn):
    rows = conn.execute(
        "SELECT r.*, s.client, s.rfp, s.submitted FROM records r JOIN sources s USING (source_id)"
        " ORDER BY r.source_id, r.sheet, r.row_num")
    with open(args.out, "w", encoding="utf-8") as f:
        n = 0
        for row in rows:
            rec = dict(row)
            for key in ("answer_raw", "attributes", "warnings"):
                rec[key] = json.loads(rec[key])
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    print(f"Wrote {n} records to {args.out}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m kb", description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"SQLite file (default {DEFAULT_DB})")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="Load workbooks described by profiles (default: all in config/profiles)")
    p.add_argument("profiles", nargs="*")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("stats", help="Show what's loaded")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("search", help="Keyword search over past requirements and answers")
    p.add_argument("query")
    p.add_argument("-n", "--limit", type=int, default=10)
    p.add_argument("--status", help="Only records with this internal status")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("export", help="Dump all records as JSONL")
    p.add_argument("out", type=Path)
    p.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    conn = store.connect(args.db)
    try:
        return args.func(args, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
