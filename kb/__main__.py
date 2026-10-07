"""Command line: python -m kb {ingest,embed,stats,search,export}."""

import argparse
import json
import sys
from pathlib import Path

from kb import settings, store
from kb.embed import Embedder
from kb.extract import extract_profile
from kb.profile import load_profile, load_scales

DEFAULT_DB = settings.DB_PATH


def _base_dir(path: Path) -> Path:
    """Dashboard-created profiles reference workbooks relative to the data dir."""
    return settings.DATA_DIR if path.resolve().is_relative_to(settings.DATA_PROFILES_DIR.resolve()) else settings.ROOT


def cmd_ingest(args, conn):
    scales = load_scales(settings.SCALES_FILE)
    locations = ([(Path(p), _base_dir(Path(p))) for p in args.profiles] if args.profiles
                 else settings.profile_locations())
    failed = False
    for path, base in locations:
        try:
            profile = load_profile(path, scales, base)
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
    if not args.no_embed:
        cmd_embed(args, conn)
    return 1 if failed else 0


def cmd_embed(args, conn):
    embedder = Embedder()
    added = store.embed_missing(conn, embedder)
    print(f"\nEmbeddings ({embedder.model_name}): {added} new" if added
          else f"\nEmbeddings ({embedder.model_name}): up to date")
    return 0


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
    for r in store.search(conn, args.query, args.limit, args.status, mode=args.mode):
        sim = f"sim={r['similarity']:.2f}, " if r["similarity"] is not None else ""
        print(f"\n[{r['status'] or '?'}] {r['client']} / {r['sheet']} {r['req_id'] or ''}"
              f"  ({sim}via {'+'.join(r['found_by'])}; raw: {r['answer_raw']})")
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


def cmd_copy_to_postgres(args, conn):
    """Copy local SQLite data (knowledge base + drafts) into the hosted Postgres database."""
    import sqlite3

    from kb import db, draft

    if not db.is_postgres(conn):
        print("Set KB_DATABASE_URL to the target Postgres database first.")
        return 1
    drafts_conn = draft.connect_drafts()
    targets = {"sources": conn, "records": conn, "embeddings": conn, "jobs": drafts_conn, "rows": drafts_conn,
               "people": drafts_conn, "modules": drafts_conn}
    if not args.replace:
        filled = [t for t in ("sources", "jobs") if targets[t].execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]]
        if filled:
            print(f"Postgres already has data in: {', '.join(filled)}. Re-run with --replace to overwrite it.")
            return 1

    sources = {"sources": args.src / "kb.sqlite", "records": args.src / "kb.sqlite",
               "embeddings": args.src / "kb.sqlite", "jobs": args.src / "drafts.sqlite",
               "rows": args.src / "drafts.sqlite", "people": args.src / "drafts.sqlite",
               "modules": args.src / "drafts.sqlite"}
    # Only vectors some record uses; old ones from removed worksheets stay behind.
    where = {"embeddings": " WHERE text_sha IN (SELECT embed_sha FROM records)"}
    with conn, drafts_conn:
        for table in ("modules", "people", "rows", "jobs", "records", "sources"):  # children before parents
            targets[table].execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM embeddings")
        for table in ("sources", "records", "embeddings", "jobs", "rows", "people", "modules"):
            path = sources[table]
            if not path.exists():
                print(f"  {table:<10} skipped ({path} not found)")
                continue
            src = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            # Columns both sides have: an older local file can carry columns since dropped.
            wanted = {r[0] for r in targets[table].execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?"
                " AND is_generated = 'NEVER'", (table,))}
            columns = [r[1] for r in src.execute(f"PRAGMA table_info({table})") if r[1] in wanted]
            if not columns:  # a file from before this table existed
                src.close()
                print(f"  {table:<10} skipped (not in {path.name})")
                continue
            rows = src.execute(f"SELECT {', '.join(columns)} FROM {table}{where.get(table, '')}").fetchall()
            src.close()
            targets[table].executemany(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})", rows)
            print(f"  {table:<10} {len(rows):>7,} rows")
    drafts_conn.close()
    print("Done. Copy the workbook folders (workbooks/, profiles/, drafts/) to KB_DATA_DIR as well.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m kb", description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"SQLite file (default {DEFAULT_DB})")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="Load workbooks described by profiles (default: all in config/profiles)")
    p.add_argument("profiles", nargs="*")
    p.add_argument("--no-embed", action="store_true", help="Skip computing embeddings after loading")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("embed", help="Compute embeddings for records that don't have one yet")
    p.set_defaults(func=cmd_embed)

    p = sub.add_parser("stats", help="Show what's loaded")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("search", help="Keyword search over past requirements and answers")
    p.add_argument("query")
    p.add_argument("-n", "--limit", type=int, default=10)
    p.add_argument("--status", help="Only records with this internal status")
    p.add_argument("--mode", choices=["hybrid", "keyword", "semantic"], default="hybrid")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("export", help="Dump all records as JSONL")
    p.add_argument("out", type=Path)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("copy-to-postgres", help="One-time move of local SQLite data into KB_DATABASE_URL")
    p.add_argument("--from", dest="src", type=Path, default=settings.DATA_DIR,
                   help="Folder holding kb.sqlite and drafts.sqlite (default KB_DATA_DIR)")
    p.add_argument("--replace", action="store_true", help="Overwrite data already in Postgres")
    p.set_defaults(func=cmd_copy_to_postgres)

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
