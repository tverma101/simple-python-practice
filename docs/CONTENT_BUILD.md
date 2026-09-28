# Building the content database

`tools/build_content.py` turns the upstream problem database into the
`content.db` that [`ducky.webapp`](../src/ducky/webapp.py) serves. It is the
bridge between the two halves of the project: `tools/verify_content.py` produces
a *report*, and this produces the *artefact the report is about*.

```bash
python tools/build_content.py --db path/to/coding_platform.db \
    --out content.db --report reports/build.json
```

| Flag | Default | Meaning |
|---|---|---|
| `--db` | required | The upstream `coding_platform.db`. Opened read-only; never written. |
| `--out` | `content.db` | The content database to write. Replaced wholesale. |
| `--report` | *(none)* | Build report path. Omit to skip. |
| `--limit N` | `0` (all) | Execute at most N reference solutions. A smoke test, not a publishable library. |
| `--jobs N` | 8 | Sandboxed verification subprocesses at a time. |
| `--pool N` | 24 | Specs held in memory per batch. |
| `--quiet` | off | Suppress progress. |

A full run takes about 75 seconds: 5,132 problems triaged, 3,275 reference
solutions executed, 5,132 rows and 30,511 test cases written.

## What ends up in the database

Every problem is written, not only the ones that ship. A quarantined problem
that is dropped is a problem a teacher cannot see, cannot prioritise, and cannot
tell apart from one that was never imported, so the store keeps the row and lets
the status separate them.

| Table | Rows | Read by the server? |
|---|---:|---|
| `problems` | 5,132 | yes — `status = 'verified'` is the student-facing list |
| `test_cases` | 30,511 | yes — visible expectations inline, hidden ones **not** |
| `hidden_expectations` | 18,007 | yes — the answer key the grader compares against |
| `solutions` | 5,114 | **no** — refused by a read-only `ContentStore` |

Reading the counts:

```python
from ducky.contentstore import ContentStore

store = ContentStore("content.db", read_only=True)
store.count()                       # 2603 -- shippable, the student-facing library
store.count(shippable_only=False)  # 5132 -- everything, for reporting
store.quarantined(limit=10_000)     # 2529 -- the teacher queue, with a reason each
```

Those are the current counts. This file previously said 2,547 / 2,585, which was
correct when it was written, and two deliberate changes account for the
movement: `TestCase.usable` now means "parses" rather than "parses and has
arguments", so a zero-argument function is no longer untestable, and the static
AST allowlist now applies to reference solutions as well as to student code,
which moves a few problems the other way. Net +56.
[`CONTENT_VERIFICATION.md`](CONTENT_VERIFICATION.md) — "Why 2,603 and not the
2,547 this file used to claim" — is the authority for that and for the
per-defect breakdown of the 2,529, so neither is restated here.

## Only `VERIFIED` ships

The build gate is `ducky.content.is_shippable`, which is an allowlist containing
exactly `ContentStatus.VERIFIED` — not a denylist of the known-bad. A problem
reaches `VERIFIED` only by executing its own reference solution against its own
test cases inside the grading sandbox and agreeing on every case. Everything else
is stored with the status and defect it earned, and `quarantined()` lists it.

## The reference solution is stored, and unreachable

`solutions` holds every reference solution, because the repair and audit tools
need them and because dropping them at build time would leave the upstream
database as the only copy of every answer. It is unreachable from the server:

```python
ContentStore("content.db", read_only=True)._conn.execute(
    "SELECT solution_code FROM solutions"
)
# sqlite3.DatabaseError: access to solutions.solution_code is prohibited
```

Enforcement is a SQLite `set_authorizer` callback installed whenever a store is
opened read-only, not a missing column and not a `WHERE` clause someone can
forget. It denies `SQLITE_READ` on the answer-key tables, so the guarantee
survives a `SELECT *` on `problems` growing a new column, a new route reaching
for the table directly, a join, or even `COUNT(*)`. A read-write handle — which
is what a teacher-side tool opens — can still get the solution deliberately:

```python
ContentStore("content.db", read_only=False).reference_solution("two-sum")
```

The asymmetry is the design. The server has no legitimate reason to hold the
answer, and a student with a valid access code is exactly the person who must
not be able to read it.

The same rule is applied to hidden test expectations. A hidden case's
`expected` is written **only** to `hidden_expectations`; the `expected` column of
`test_cases` is left `NULL` for it, and `load_problem` reads it back from the
answer-key table. Grading is unaffected — it has to compare against the hidden
answers — but a query against `test_cases` can no longer return one.

## The build report

`--report` writes JSON with everything a reader needs to decide whether to ship
the artefact.

* **`run`** — timestamp, input and output paths, wall clock, and a per-phase
  timing breakdown. This is the *only* block that moves between two runs of the
  same content, which is what makes the rest diffable.
* **`totals`** — problems, shipped, quarantined, executed.
* **`counts`** — row counts per table, read back from the file that was written
  rather than from the numbers the build intended to write, plus the
  `cases_total` / `cases_parsed` / `cases_usable` split. `parsed` and `usable`
  are different numbers and the gap is a real content gap: 920 cases decode
  cleanly but pass no argument at all, so there is nothing to call.
* **`status_breakdown`** / **`defect_breakdown`** — the two views a teacher
  needs: can this ship, and what has to change. Both sorted by count then name,
  so a diff is readable.
* **`quarantined`** — one row per non-shippable problem, with its status, defect,
  and detail.
* **`fingerprint`** — `sha256` of everything except `run`. One line answers "did
  the library change?".

Diffing two runs:

```bash
jq 'del(.run)' reports/build-a.json > a.json
jq 'del(.run)' reports/build-b.json > b.json
diff a.json b.json
```

**The build is idempotent.** Same input, byte-identical output:

```bash
python tools/build_content.py --db upstream.db --out one.db --report a.json
python tools/build_content.py --db upstream.db --out two.db --report b.json
shasum -a 256 one.db two.db   # identical
```

## The build audits itself

After writing, the build reopens the file and checks it says what the build
intended: row counts per table, that the shipped and quarantined counts add up to
the row count, that no `VERIFIED` problem has no test case, that no hidden
expectation is inlined in `test_cases`, that every hidden case has an answer-key
row, and that a read-only store still cannot read the answer key. A failure is
printed and the tool exits non-zero — a store that disagrees with its own report
is worse than no store, because its provenance cannot be described.

## The store and the report cannot disagree

Both the status and the defect written to the database come from the same
`VerificationResult` the counts come from. For problems rejected without
execution, the defect rule is *imported* from `tools/verify_content.py` rather
than copied, because this project has already had the two drift apart once and
the result was a tool that quietly did nothing.
`tests/test_contentstore.py` asserts the sharing, so a copy cannot creep back in.

## See also

* [`CONTENT_VERIFICATION.md`](CONTENT_VERIFICATION.md) — what is wrong with the
  library, and what has to be fixed to shrink the quarantine.
* [`SECURITY_MODEL.md`](SECURITY_MODEL.md) — what the sandbox does and does not
  stop, and why verification runs inside it.
