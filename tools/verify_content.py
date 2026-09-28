#!/usr/bin/env python3
"""Verify the imported problem library by actually running it.

Reads the upstream Python-practice-platform SQLite database, normalises it
(:mod:`ducky.content`), executes every reference solution against its own test
cases inside the grading sandbox (:mod:`ducky.verify`), and writes a quarantine
report.

The report is the point. A number like "5,132 problems" says nothing about
whether a single one can be graded; this produces the number that does, plus a
per-reason breakdown so the gaps can be prioritised instead of guessed at.

Usage:
    python tools/verify_content.py --db path/to/coding_platform.db \\
        --out reports/verification.json [--limit N] [--jobs N] [--pool N]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ducky.content import (  # noqa: E402
    BLOCKING_STATUSES,
    ContentStatus,
    Defect,
    classify_static,
    iter_problems,
)
from ducky.verify import VERIFICATION_POOL_SIZE, verify_all  # noqa: E402

#: Only these are worth spending a subprocess on. Everything else is already
#: decided by inspection, and running it would just re-confirm a parse error.
_EXECUTABLE = frozenset({ContentStatus.UNVERIFIED})


def _static_defect(status: ContentStatus) -> str:
    """Best defect for a problem rejected without executing anything."""
    if status is ContentStatus.STARTER_BROKEN:
        return Defect.STARTER_UNPARSEABLE.value
    return Defect.ROW_UNPARSEABLE.value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="path to coding_platform.db")
    parser.add_argument("--out", default="reports/verification.json")
    parser.add_argument("--limit", type=int, default=0, help="verify at most N problems")
    parser.add_argument("--jobs", type=int, default=VERIFICATION_POOL_SIZE)
    parser.add_argument(
        "--pool",
        type=int,
        default=24,
        help="how many verified specs to hold in memory at once",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    db = Path(args.db)
    if not db.is_file():
        print(f"error: no such database: {db}", file=sys.stderr)
        return 2

    started = time.monotonic()

    # Pass 1: static triage over everything. Cheap, and it decides most rows.
    all_specs = []
    static_counts: Counter[str] = Counter()
    for spec in iter_problems(db):
        classify_static(spec)
        static_counts[spec.status.value] += 1
        all_specs.append(spec)

    candidates = [s for s in all_specs if s.status in _EXECUTABLE]
    if args.limit:
        candidates = candidates[: args.limit]

    if not args.quiet:
        print(f"triaged {len(all_specs)} problems in {time.monotonic() - started:.1f}s")
        for status, count in static_counts.most_common():
            print(f"  {count:>5}  {status}")
        print(f"\nexecuting {len(candidates)} reference solutions "
              f"({args.jobs} at a time)...", flush=True)

    # Pass 2: execute, in bounded batches so a 5,132-problem run does not hold
    # every spec and every result in memory at once.
    last_tick = [0.0]

    def progress(done: int, total: int) -> None:
        now = time.monotonic()
        if args.quiet or (now - last_tick[0] < 5 and done != total):
            return
        last_tick[0] = now
        rate = done / max(1e-6, now - started - static_elapsed)
        remaining = (total - done) / rate if rate else 0
        print(f"  {done}/{total}  ({rate:.0f}/s, ~{remaining:.0f}s left)", flush=True)

    static_elapsed = time.monotonic() - started
    results = []
    batch: list = []
    for spec in candidates:
        batch.append(spec)
        if len(batch) >= args.pool:
            results.extend(verify_all(batch, pool_size=args.jobs))
            batch = []
            progress(len(results), len(candidates))
    if batch:
        results.extend(verify_all(batch, pool_size=args.jobs))
        progress(len(results), len(candidates))

    exec_counts: Counter[str] = Counter(r.status.value for r in results)

    # Combine: a statically-rejected problem keeps its static verdict; otherwise
    # the executed verdict wins, since running the code outranks reading it.
    by_source = {r.source_id: r for r in results}
    final_counts: Counter[str] = Counter()
    combined: list[dict] = []
    for spec in all_specs:
        executed = by_source.get(spec.source_id)
        if executed is not None:
            status = executed.status
            detail = executed.detail
        else:
            status = spec.status
            detail = spec.status_detail
        final_counts[status.value] += 1
        combined.append(
            {
                "slug": spec.slug,
                "source_id": spec.source_id,
                "title": spec.title,
                "unit": spec.unit,
                "topic_tier": spec.topic_tier,
                "difficulty": spec.difficulty,
                "status": status.value,
                "defect": (executed.defect.value if executed is not None
                           else _static_defect(status)),
                "detail": detail,
                "shippable": status not in BLOCKING_STATUSES and bool(spec.test_cases),
                "cases_total": len(spec.test_cases),
                "cases_usable": sum(1 for t in spec.test_cases if t.usable),
                "executed": executed is not None,
            }
        )

    shippable = sum(1 for row in combined if row["shippable"])
    blocking = [row for row in combined if not row["shippable"]]

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source_db": str(db),
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "totals": {
            "problems": len(combined),
            "shippable": shippable,
            "quarantined": len(blocking),
            "shippable_percent": round(100 * shippable / max(1, len(combined)), 2),
            "executed": len(results),
        },
        "status_breakdown": dict(final_counts.most_common()),
        "static_breakdown": dict(static_counts.most_common()),
        "executed_breakdown": dict(exec_counts.most_common()),
        "quarantine_by_status": dict(
            Counter(row["status"] for row in blocking).most_common()
        ),
        # The actionable view: what a human has to change, which does not
        # line up one-to-one with status.
        "quarantine_by_defect": dict(
            Counter(row["defect"] for row in blocking).most_common()
        ),
        "quarantined": sorted(blocking, key=lambda r: (r["status"], r["slug"])),
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if not args.quiet:
        print(f"\n{'=' * 62}")
        print(f"shippable : {shippable} / {len(combined)}  "
              f"({report['totals']['shippable_percent']}%)")
        print(f"quarantine: {len(blocking)}")
        print(f"{'=' * 62}")
        print("\nquarantine by defect (what a human must fix):")
        for defect, count in Counter(row["defect"] for row in blocking).most_common():
            print(f"  {count:>5}  {defect}")
        print("\nfinal status breakdown:")
        for status, count in final_counts.most_common():
            marker = "  " if status == ContentStatus.VERIFIED.value else "  ! "
            print(f"{marker}{count:>5}  {status}")
        print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
