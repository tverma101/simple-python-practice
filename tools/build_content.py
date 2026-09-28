#!/usr/bin/env python3
"""Build the servable content database from the upstream problem library.

Reads the upstream Python-practice-platform SQLite database, normalises it
(:mod:`ducky.content`), verifies every reference solution against its own test
cases inside the grading sandbox (:mod:`ducky.verify`), and writes a
``content.db`` that :mod:`ducky.contentstore` can serve. This is the missing link
between the two halves of the project: ``tools/verify_content.py`` produces a
report, and this produces the artefact the report is a report *about*.

Three rules the build is built around:

* **Only ``VERIFIED`` ships, and nothing is dropped.** Every problem is written
  with the status it earned, so :meth:`ContentStore.quarantined` can hand a
  teacher a queue instead of the build silently deleting half the library. A
  quarantined problem a teacher cannot see is a problem that looks identical to
  one that was never imported.
* **The store and the report agree.** The status, detail, and defect in the
  database are the ones from the same :class:`~ducky.verify.VerificationResult`
  the counts come from, so a diff of two reports is a diff of two databases.
* **The reference solution is stored and unreachable.** Solutions go in their own
  table, which a read-only :class:`~ducky.contentstore.ContentStore` is refused
  by SQLite authorizer. See ``docs/CONTENT_BUILD.md``.

The build is idempotent: same input, same bytes out, so a second run can be
diffed against the first. Compare two reports with everything but the ``run``
block -- the wall clock, the input path, and the output path are the only things
that legitimately move between runs:

    jq 'del(.run)' reports/build-a.json > a.json
    jq 'del(.run)' reports/build-b.json > b.json
    diff a.json b.json

Usage:
    python tools/build_content.py --db path/to/coding_platform.db \\
        --out content.db [--report reports/build.json] [--limit N] [--jobs N]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src"))
# verify_content is imported, not copied. The rule for the defect of a problem
# that was rejected without running anything lives there, and when this tool
# grew its own copy of it the two would drift -- which is exactly how the repair
# tool came to decline the problems it was written to fix.
sys.path.insert(0, str(_HERE))

from verify_content import _static_defect  # noqa: E402

from ducky.content import (  # noqa: E402
    ContentStatus,
    classify_static,
    is_shippable,
    iter_problems,
)
from ducky.contentstore import ContentStore, Verdict, build_store  # noqa: E402
from ducky.verify import VERIFICATION_POOL_SIZE, verify_all  # noqa: E402

#: Only these are worth spending a subprocess on. Everything else is already
#: decided by inspection, and running it would just re-confirm a parse error.
_EXECUTABLE = frozenset({ContentStatus.UNVERIFIED})

#: Ceiling for the ``quarantined()`` audit read. Above the library, so the count
#: is the count.
_AUDIT_LIMIT = 100_000


def _tally(counter: Counter) -> dict[str, int]:
    """Counts as a dict, most common first, ties broken by name.

    ``Counter.most_common`` breaks ties by insertion order, which is fine but
    makes a diff harder to read: a tie that swaps is a two-line diff either way,
    whereas ordering by name keeps the same rows in the same place across runs.
    """
    return {k: counter[k] for k in sorted(counter, key=lambda k: (-counter[k], k))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="path to coding_platform.db")
    parser.add_argument("--out", default="content.db", help="content database to write")
    parser.add_argument("--report", default="", help="write a build report here (default: none)")
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
    import_seconds = time.monotonic() - started

    candidates = [s for s in all_specs if s.status in _EXECUTABLE]
    partial = bool(args.limit)
    if partial:
        candidates = candidates[: args.limit]

    if not args.quiet:
        print(f"triaged {len(all_specs)} problems in {import_seconds:.1f}s")
        for status, count in static_counts.most_common():
            print(f"  {count:>5}  {status}")
        print(f"\nexecuting {len(candidates)} reference solutions "
              f"({args.jobs} at a time)...", flush=True)
        if partial:
            print(f"NOTE: --limit {args.limit} -- {len(all_specs)} problems are written but "
                  f"only {len(candidates)} were executed, so the rest stay UNVERIFIED and "
                  f"quarantine. This store is a smoke test, not a publishable library.",
                  flush=True)

    # Pass 2: execute, in bounded batches so a 5,132-problem run does not hold
    # every spec and every result in memory at once.
    def progress(done: int, total: int) -> None:
        now = time.monotonic()
        if args.quiet or (now - last_tick[0] < 5 and done != total):
            return
        last_tick[0] = now
        rate = done / max(1e-6, now - started - import_seconds)
        remaining = (total - done) / rate if rate else 0
        print(f"  {done}/{total}  ({rate:.0f}/s, ~{remaining:.0f}s left)", flush=True)

    last_tick = [0.0]
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
    verify_seconds = time.monotonic() - started - import_seconds

    # One verdict per problem, from the same object the report counts come from.
    # A statically-rejected problem never got a VerificationResult, so it keeps
    # its static verdict and the same defect rule verify_content.py applies --
    # which is what makes the two reports diffable against each other.
    by_source = {r.source_id: r for r in results}
    verdicts: dict[int, Verdict] = {}
    final_counts: Counter[str] = Counter()
    defect_counts: Counter[str] = Counter()
    quarantined: list[dict] = []
    cases_total = 0
    cases_parsed = 0
    cases_usable = 0
    cases_hidden = 0

    for spec in all_specs:
        executed = by_source.get(spec.source_id)
        if executed is not None:
            verdict = Verdict(executed.status, executed.detail, executed.defect.value)
        else:
            verdict = Verdict(
                spec.status, spec.status_detail, _static_defect(spec.status)
            )
        verdicts[spec.source_id] = verdict
        final_counts[verdict.status.value] += 1
        cases_total += len(spec.test_cases)
        cases_parsed += sum(1 for tc in spec.test_cases if not tc.parse_error)
        cases_usable += sum(1 for tc in spec.test_cases if tc.usable)
        cases_hidden += sum(1 for tc in spec.test_cases if tc.is_hidden)
        if not is_shippable(ContentStatus(verdict.status)):
            defect_counts[verdict.defect] += 1
            quarantined.append(
                {
                    "slug": spec.slug,
                    "source_id": spec.source_id,
                    "title": spec.title,
                    "difficulty": spec.difficulty,
                    "topic_tier": spec.topic_tier,
                    "status": verdict.status,
                    "defect": verdict.defect,
                    "detail": verdict.detail,
                    "cases_total": len(spec.test_cases),
                    "cases_usable": sum(1 for tc in spec.test_cases if tc.usable),
                }
            )

    shipped = len(all_specs) - len(quarantined)
    exec_counts = Counter(r.status.value for r in results)

    # Pass 3: write, then read the file back and check it says what we meant.
    # Two handles on purpose. `builder` is the build's own, read-write, and is the
    # only one that can count the answer key; `reader` is exactly the handle the
    # server gets, and the audit checks that the two agree on everything the
    # server can see.
    out = Path(args.out)
    builder = build_store(all_specs, out, verdicts=verdicts)
    write_seconds = time.monotonic() - started - import_seconds - verify_seconds

    reader = ContentStore(out, read_only=True)
    try:
        rows = builder.table_counts()
        shipped_in_db = reader.count()
        quarantined_in_db = len(reader.quarantined(limit=_AUDIT_LIMIT))
        if rows["solutions"] is None:  # pragma: no cover - builder is read-write
            raise AssertionError("the builder's own handle cannot count solutions")
        audit = _audit(builder, reader, all_specs, rows, shipped, len(quarantined))
    finally:
        reader.close()
        builder.close()
    audit_seconds = time.monotonic() - started - import_seconds - verify_seconds - write_seconds

    report = {
        # Everything here moves between two runs of the same content. It is
        # grouped so that "diff two runs" is one `del(.run)`.
        "run": {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source_db": str(db),
            "out": str(out),
            "partial": partial,
            "jobs": args.jobs,
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "phases": {
                "import_seconds": round(import_seconds, 1),
                "verify_seconds": round(verify_seconds, 1),
                "write_seconds": round(write_seconds, 1),
                "audit_seconds": round(audit_seconds, 1),
            },
        },
        "totals": {
            "problems": len(all_specs),
            "shipped": shipped,
            "quarantined": len(quarantined),
            "shipped_percent": round(100 * shipped / max(1, len(all_specs)), 2),
            "executed": len(results),
        },
        # Top-level counts, read back from the file that was written rather than
        # from the numbers the build intended to write.
        "counts": {
            "problems": rows["problems"],
            "test_cases": rows["test_cases"],
            "hidden_cases": rows["hidden_expectations"],
            "solutions": rows["solutions"],
            # `parsed` and `usable` are different numbers and the gap is a real
            # content gap: 920 cases decode cleanly but pass no argument at all,
            # so there is nothing to call. Reported separately so a reader is not
            # left guessing which one a document is quoting.
            "cases_total": cases_total,
            "cases_parsed": cases_parsed,
            "cases_usable": cases_usable,
            "cases_hidden": cases_hidden,
            "rows": rows,
        },
        "status_breakdown": _tally(final_counts),
        "defect_breakdown": _tally(defect_counts),
        "quarantine_by_status": _tally(Counter(r["status"] for r in quarantined)),
        "quarantine_by_defect": _tally(Counter(r["defect"] for r in quarantined)),
        "static_breakdown": _tally(static_counts),
        "executed_breakdown": _tally(exec_counts),
        "audit": {"failures": audit},
        "quarantined": sorted(quarantined, key=lambda r: (r["status"], r["slug"])),
    }
    report["fingerprint"] = _fingerprint(report)

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if not args.quiet:
        print(f"\n{'=' * 62}")
        print(f"wrote {out}  ({out.stat().st_size / 1e6:.1f} MB)")
        print(f"shipped  : {shipped_in_db} / {len(all_specs)}  "
              f"({report['totals']['shipped_percent']}%)")
        print(f"quarantine: {quarantined_in_db}")
        print(f"test cases: {rows['test_cases']} "
              f"({cases_usable} usable, {cases_hidden} hidden)")
        print(f"solutions  : {rows['solutions']} (not readable from a read-only store)")
        print(f"{'=' * 62}")
        print("\nquarantine by defect (what a human must fix):")
        for defect, count in report["quarantine_by_defect"].items():
            print(f"  {count:>5}  {defect}")
        print("\nstatus breakdown:")
        for status, count in report["status_breakdown"].items():
            marker = "  " if status == ContentStatus.VERIFIED.value else "  ! "
            print(f"{marker}{count:>5}  {status}")
        if audit:
            print(f"\naudit FAILED ({len(audit)} problems with the file):")
            for line in audit:
                print(f"  ! {line}")
        else:
            print("\naudit ok: the file says what the build intended")
        print(f"fingerprint: {report['fingerprint']}")
        if args.report:
            print(f"wrote {args.report}")

    return 1 if audit else 0


def _fingerprint(report: dict) -> str:
    """Hash of everything in a report except the run block.

    A one-line way to ask "did the library change?" without reading 2,582 rows of
    quarantine detail.
    """
    stable = {k: v for k, v in report.items() if k not in ("run", "fingerprint")}
    blob = json.dumps(stable, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def _audit(
    builder: ContentStore,
    reader: ContentStore,
    specs: list,
    rows: dict[str, int],
    shipped: int,
    quarantined: int,
) -> list[str]:
    """Read the built database back and complain about anything inconsistent.

    A build that writes a store disagreeing with its own report is worse than one
    that fails: it ships a library whose provenance nobody can describe. These are
    the invariants that would catch it, checked against the file rather than
    against the build's own bookkeeping.
    """
    failures: list[str] = []
    expected_cases = sum(len(s.test_cases) for s in specs)
    expected_hidden = sum(
        1 for s in specs for tc in s.test_cases if tc.is_hidden
    )
    expected_solutions = sum(1 for s in specs if s.solution_code)

    if rows["problems"] != len(specs):
        failures.append(
            f"problems table has {rows['problems']} rows, build wrote {len(specs)} "
            f"specs -- {len(specs) - rows['problems']} lost to a slug collision"
        )
    if rows["test_cases"] != expected_cases:
        failures.append(
            f"test_cases has {rows['test_cases']} rows, expected {expected_cases}"
        )
    if rows["hidden_expectations"] != expected_hidden:
        failures.append(
            f"hidden_expectations has {rows['hidden_expectations']} rows, "
            f"expected {expected_hidden}"
        )
    if rows["solutions"] != expected_solutions:
        failures.append(
            f"solutions has {rows['solutions']} rows, expected {expected_solutions}"
        )

    # Counted through the read-only handle, because that is the count the server
    # would give. If the two handles disagree the store is not self-consistent.
    shipped_in_db = reader.count()
    quarantined_in_db = len(reader.quarantined(limit=_AUDIT_LIMIT))
    if shipped_in_db != shipped:
        failures.append(f"store ships {shipped_in_db}, build decided {shipped}")
    if quarantined_in_db != quarantined:
        failures.append(f"store quarantines {quarantined_in_db}, build decided {quarantined}")
    if shipped_in_db + quarantined_in_db != rows["problems"]:
        failures.append(
            f"{shipped_in_db} shipped + {quarantined_in_db} quarantined != "
            f"{rows['problems']} rows"
        )

    def scalar(sql: str, *params) -> int:
        return int(builder._conn.execute(sql, params).fetchone()[0])

    verified_wo_cases = scalar(
        """SELECT COUNT(*) FROM problems p WHERE p.status = ? AND NOT EXISTS
           (SELECT 1 FROM test_cases t WHERE t.slug = p.slug)""",
        ContentStatus.VERIFIED.value,
    )
    if verified_wo_cases:
        failures.append(
            f"{verified_wo_cases} problem(s) marked verified carry no test case, "
            f"so nothing could have been checked"
        )
    hidden_inlined = scalar(
        "SELECT COUNT(*) FROM test_cases WHERE is_hidden = 1 AND expected IS NOT NULL"
    )
    if hidden_inlined:
        failures.append(
            f"{hidden_inlined} hidden expectation(s) are inlined in test_cases; the "
            f"answer key is supposed to live only in hidden_expectations"
        )
    missing_answer = scalar(
        """SELECT COUNT(*) FROM test_cases t WHERE t.is_hidden = 1 AND NOT EXISTS
           (SELECT 1 FROM hidden_expectations h WHERE h.slug = t.slug AND h.idx = t.idx)"""
    )
    if missing_answer:
        failures.append(
            f"{missing_answer} hidden case(s) have no row in hidden_expectations, "
            f"so grading them would compare against nothing"
        )

    # The whole point of the answer key: a handle the server holds must not be
    # able to read it, by any of the routes a query could take.
    for sql in (
        "SELECT solution_code FROM solutions",
        "SELECT * FROM solutions",
        "SELECT s.solution_code FROM solutions s JOIN problems p ON p.slug = s.slug",
    ):
        try:
            reader._conn.execute(sql).fetchall()
        except sqlite3.DatabaseError:
            continue
        failures.append(f"a read-only ContentStore could read the answer key: {sql!r}")
    try:
        reader.reference_solution(specs[0].slug)
    except PermissionError:
        pass
    else:
        failures.append(
            "read-only ContentStore.reference_solution() returned a solution; the "
            "answer key is reachable from the server"
        )
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
