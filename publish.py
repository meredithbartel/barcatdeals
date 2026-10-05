#!/usr/bin/env python3
"""Publish the current deals to the live site.

Copies ``data/bardeals.db`` into ``deploy/bardeals.db`` - the snapshot the
hosted site serves - leaving out sample/demo rows and the raw LLM parse log,
then commits and pushes it. Render redeploys on every push to ``main``.

    python publish.py              # snapshot, commit, push
    python publish.py --no-push    # snapshot and commit only
    python publish.py --dry-run    # snapshot only; show what would go live

The local database is only read, never modified.
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from bardeals.config import get_config  # noqa: E402
from bardeals.web import is_sample_source  # noqa: E402

SNAPSHOT = ROOT / "deploy" / "bardeals.db"


def snapshot(source: Path, target: Path = SNAPSHOT) -> dict[str, int]:
    """Write a public copy of ``source`` to ``target`` and return row counts.

    Uses SQLite's backup API, so a scrape writing at the same moment cannot
    produce a torn copy.
    """
    if not source.exists():
        raise SystemExit(f"No database at {source}. Run run_daily.py first.")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".tmp")
    temp.unlink(missing_ok=True)
    # sqlite3's context manager commits but does not close; Windows then
    # refuses to rename a file that is still open.
    live, copy = sqlite3.connect(source), sqlite3.connect(temp)
    try:
        live.backup(copy)
    finally:
        live.close()
        copy.close()
    copy = sqlite3.connect(temp)
    try:
        # Demo rows invent deals at real bars; they must never reach the public site.
        sample = [code for code, link in copy.execute("SELECT shortcode, permalink FROM posts")
                  if is_sample_source(link, code)]
        sample += [code for (code,) in copy.execute("SELECT DISTINCT shortcode FROM deals")
                   if is_sample_source(None, code) and code not in sample]
        copy.executemany("DELETE FROM deals WHERE shortcode = ?", [(c,) for c in sample])
        copy.executemany("DELETE FROM posts WHERE shortcode = ?", [(c,) for c in sample])
        # Raw model responses are debugging material, not something to publish.
        copy.execute("DELETE FROM parse_log")
        copy.commit()
        copy.execute("PRAGMA journal_mode = DELETE")
        copy.execute("VACUUM")
        counts = {
            "deals": copy.execute("SELECT COUNT(*) FROM deals").fetchone()[0],
            "posts": copy.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
            "sample posts removed": len(sample),
        }
    finally:
        copy.close()
    temp.replace(target)
    return counts


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-push", action="store_true", help="Commit but do not push.")
    parser.add_argument("--dry-run", action="store_true", help="Write the snapshot only; no git.")
    args = parser.parse_args()

    counts = snapshot(get_config().db_path)
    print(", ".join(f"{value} {key}" for key, value in counts.items()))
    if args.dry_run:
        print(f"Snapshot written to {SNAPSHOT.relative_to(ROOT)}; nothing committed.")
        return 0

    git("add", str(SNAPSHOT.relative_to(ROOT)))
    if git("diff", "--cached", "--quiet").returncode == 0:
        print("Live data is already up to date.")
    else:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        result = git("commit", "-m", f"Publish deals data ({stamp})")
        if result.returncode != 0:
            print(result.stdout + result.stderr, file=sys.stderr)
            return 1
        print(f"Committed data snapshot ({stamp}).")
    if args.no_push:
        return 0
    result = git("push")
    if result.returncode != 0:
        print(result.stdout + result.stderr, file=sys.stderr)
        return 1
    print("Pushed. Render will redeploy in a minute or two.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
