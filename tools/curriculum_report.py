"""Re-derive the lesson-graph numbers in ``docs/UX_DECISIONS.md`` section 5.

Read-only, and deliberately not wired into the test suite: the numbers describe
one specific source database, and a test that asserts them would fail the moment
the content is re-imported. Run it by hand when the data changes, or when a
decision in that document needs checking rather than trusting.

    python tools/curriculum_report.py --db coding_platform.db \\
        --verification reports/verification.json

With ``--verification`` the report marks which linked problems are verified and
therefore whether any lesson can be recommended. Without it, nothing is verified
and the content-floor section is reported as empty on purpose -- a graph with no
verification data is not a graph with no content.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ducky.curriculum import MIN_CONTENT, Curriculum, recommend_next  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="source database (read-only)")
    parser.add_argument(
        "--verification",
        help="verification report JSON; its quarantined slugs decide what is verified",
    )
    parser.add_argument(
        "--content",
        help="content database, for the practice-pool numbers. Without it the "
        "fallback has no pool and says so.",
    )
    parser.add_argument("--unresolved", action="store_true", help="list every unresolved token")
    parser.add_argument("--empty", action="store_true", help="list every lesson below the floor")
    return parser.parse_args(argv)


def verified_slugs(db: Path, report: Path | None) -> set[str]:
    if report is None:
        return set()
    quarantined = {
        row["slug"] for row in json.loads(report.read_text())["quarantined"]
    }
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        imported = {row[0] for row in conn.execute("SELECT slug FROM coding_problems WHERE is_active = 1")}
    finally:
        conn.close()
    return imported - quarantined


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    db = Path(args.db)
    if not db.exists():
        print(f"no database at {db}", file=sys.stderr)
        return 2

    store = None
    if args.content:
        from ducky.contentstore import ContentStore

        store = ContentStore(args.content, read_only=True)

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        curriculum = Curriculum.from_source(
            conn, store=store, verified=verified_slugs(db, Path(args.verification) if args.verification else None)
        )
    finally:
        conn.close()
    if store is not None:
        store.close()

    print(f"graph: {db}")
    for key, value in sorted(curriculum.stats.items()):
        print(f"  {key}: {value}")
    print(f"  roots: {len(curriculum.roots())} -> {[lesson.key for lesson in curriculum.roots()]}")
    print(f"  max_depth: {curriculum.max_depth}")

    edges = curriculum.edges
    backward_id = sum(
        1
        for edge in edges
        if edge.parent_active
        and edge.child != edge.parent
        and curriculum.lesson(edge.parent).lesson_id >= curriculum.lesson(edge.child).lesson_id
    )
    pair = {lesson.key: (lesson.unit_num, lesson.lesson_num) for lesson in curriculum.lessons}
    backward_pair = sum(
        1
        for edge in edges
        if edge.parent_active
        and edge.child != edge.parent
        and pair[edge.parent] >= pair[edge.child]
    )
    total = sum(1 for edge in edges if edge.parent_active and edge.child != edge.parent)
    print(f"  backward edges under raw id:        {backward_id}/{total}")
    print(f"  backward edges under (unit, lesson): {backward_pair}/{total}")

    if args.unresolved:
        print("unresolved prerequisites:")
        for item in curriculum.unresolved:
            print(f"  {item.child}: {item.raw!r} -- {item.reason}")

    print(f"content floor (MIN_CONTENT = {MIN_CONTENT}):")
    for lesson in curriculum.empty_lessons():
        print(
            f"  {lesson.key} linked={len(lesson.problems):>3} "
            f"verified={lesson.verified_count:>3} usable={lesson.usable_count:>3} {lesson.slug}"
        )
    print(f"  lessons below the floor: {len(curriculum.empty_lessons())}/{len(curriculum.lessons)}")

    rec = recommend_next({}, curriculum)
    print(f"recommendation for a new student: {rec}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
