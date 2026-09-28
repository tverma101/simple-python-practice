# Contributing

Thanks for looking. This is a small project with one line of development, so
small, real fixes are worth more than large speculative ones.

## The one rule that matters most

**A new check on the sandbox or the web layer must be shown to fail against the
code it guards, before it is shown to pass.**

Not "the test passes." Demonstrate the test failing first — revert or weaken the
guard, run the test, paste the failure, then restore the guard. A test that has
never failed is not evidence of anything, and this repository has shipped real
defects with the whole suite green:

* Every Run and Submit returned 401. The tests all authenticated by putting
  `access_code` in the JSON body — a payload the browser never sends — so the
  primary feature was broken and the suite was green.
* Autosave reported "Saved" while storing nothing: the route read a store that
  was never assigned and returned 200 with `saved:false`, and the test checked
  only the HTTP status.
* `open('/etc/passwd').read()` returned the file's contents inside the grading
  response, because the grader echoes the submission's own value back to the
  browser.
* `GEMINI_API_KEY` and the real `~/.ducky/config.json` were readable from
  inside a graded run, because the child inherited the parent environment.
* The answer key was exfiltrable through a diagnostic string that quoted the
  tail of a truncated 256 KB output line, with no sandbox escape at all.

If you cannot make it fail, you have not found the bug yet, or the guard is not
where you think it is. Both are worth knowing.

## Setup

```bash
git clone https://github.com/tverma101/simple-python-practice.git
cd simple-python-practice
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Python 3.10+ is the floor and is the version that must never break.

## Run the tests

```bash
python -m pytest
```

The full suite takes a couple of minutes — a large part of it is real code being
sandboxed and executed on purpose.

The browser checks are separate, because they need a real browser:

```bash
python tools/check_editor.py
```

It drives Chrome (or Chromium) and asserts on rendered DOM, not on console
output. It exists because CodeMirror 6 fails *silently*: a bare import specifier
missing from `static/vendor/cm/importmap.json` yields a dead editor with no error
of any kind, which looks correct in a screenshot. If Chrome is not installed the
check cannot run; say so in the pull request rather than claiming it passed.

## Lint

```bash
.venv/bin/ruff check src tests tools
```

CI runs `ruff check src tests` on every push. The rule set is pinned explicitly
in `pyproject.toml` on purpose, so a ruff upgrade cannot silently redefine what
"lint passes" means. If you add a rule to that list, add it deliberately.

## What CI runs

`.github/workflows/tests.yml` has four jobs: lint, the test suite on Python
3.10-3.13 on Linux plus 3.12 on macOS, the vendored-editor check in Chromium, and
a job that validates every bundled quiz against the schema.

## Where things live

| Path | What it is |
|---|---|
| `src/ducky/sandbox.py` | The four containment layers, and what they refuse |
| `src/ducky/runner.py` | Execution, timeouts, output capture, process-group kill |
| `src/ducky/webapp.py` | The stdlib HTTP server, guards, rate limiting |
| `src/ducky/routes.py` | Every route, and which ones require an access code |
| `src/ducky/verify.py` | Content verification — runs inside the same sandbox |
| `src/ducky/contentstore.py` | Read-only store, and the SQLite authorizer that denies the answer key |
| `tools/build_content.py` | Builds the `content.db` the server serves |
| `tools/check_editor.py` | The browser check |
| `static/vendor/cm/` | Vendored CodeMirror 6 — do not edit, do not reformat |

## House rules

* **Do not soften a security claim to make a test pass.** If a protection is
  absent, say so in `docs/SECURITY_MODEL.md` and in the runtime summary. The
  project reports what the kernel actually did, not what we asked for, and that
  is deliberate.
* **Do not weaken `TestStaticFilterAllows`.** It pins the legitimate Python that
  the allowlist must not reject. Loosening the filter should show up as a visible
  failure there, not as a new hole.
* **A fix for a defect comes with the test that would have caught it**, written
  to the fail-first standard above.
* **Do not edit `static/vendor/cm/`.** It is unmodified upstream CodeMirror 6.
  Update it by replacing the bundles and their `.LICENSE` files together.
* **Keep runtime dependencies at zero beyond `pytest`.** No web framework, no
  bundler, no remote assets. The server is stdlib and the editor is vendored on
  purpose.
* **Do not commit `*.db`.** Content databases, classroom databases and run logs
  are gitignored, and the classroom database holds access codes.
* **One concern per pull request.** A sandbox fix and a refactor in the same diff
  is two pull requests.

## Adding a problem

Problems are not added by hand. They are imported from the upstream
`coding_platform.db`, verified by executing each reference solution against its
own test cases inside the sandbox, and marked shippable only if they agree
everywhere. See [`docs/CONTENT_BUILD.md`](docs/CONTENT_BUILD.md) and
[`docs/CONTENT_VERIFICATION.md`](docs/CONTENT_VERIFICATION.md). Do not bypass
that pipeline by inserting a row; the gate is the point.

## Security issues

Not here. See [SECURITY.md](SECURITY.md) — a sandbox escape must be reported
privately, not as a public issue.

## Code of Conduct

[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) applies, in the repository and in the
project's other community spaces.
