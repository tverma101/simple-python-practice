# Simple Python Practice

A local-first web platform for learning Python by writing and submitting
exercises. A student opens a problem in the browser, writes Python in an
embedded editor, submits it, and is told which tests failed and why. The code is
graded by running it, on the machine that hosts the server, against verified test
cases.

It is built for two people:

* **A teacher** running a classroom. One machine serves the whole room over the
  local network. Students get their own access code, so every run is
  attributable to a person and one student cannot saturate the grader.
* **A student** practising. Problems are self-contained, grading is instant, and
  the practice server itself sends nothing anywhere. There is no account to
  create, no cloud service behind it, and no telemetry in it.

  Two honest caveats. The installed package checks a release manifest over the
  network on launch so the auto-updater can work; that belongs to the `ducky`
  distribution, not to the practice server. There is no CLI flag for it — set
  `"auto_update_app": false` in `~/.ducky/config.json` to stop it. And the
  hinting code in the wider `ducky` package can talk to a local Ollama/LM Studio
  or a hosted model if you configure it; the practice server does not use it.

## What it is not

**It is not a multi-tenant judge.** It runs untrusted student code on the host
machine, and the sandbox that contains that code is layered but **not a hardened
security boundary**. There is no uid drop, no seccomp, no Landlock, and no
network isolation; on macOS a memory bomb is bounded only by the wallclock
timeout. Read [`docs/SECURITY_MODEL.md`](docs/SECURITY_MODEL.md) before you put
this on any network you do not control, and before you accept code from anyone
you would not hand a shell prompt to. That document states the gaps as plainly
as it states the protections, and it is the honest description of this project.

To report a sandbox or web-layer problem, see [SECURITY.md](SECURITY.md) — and
note that a sandbox escape must **not** be reported as a public issue.

## Requirements

* Python 3.10 or newer
* A web browser. Nothing else — no npm, no bundler, no external services.

Runtime dependencies are `pytest` and the standard library. The editor is
CodeMirror 6, vendored under `static/vendor/cm/`.

## Install

```bash
git clone https://github.com/tverma101/simple-python-practice.git
cd simple-python-practice
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Build the problem library

The problem database is **not** in this repository. `tools/build_content.py`
reads the upstream `coding_platform.db` produced by the Python-practice-platform
project and writes the `content.db` the server serves:

```bash
python tools/build_content.py --db <source.db> --out ~/.ducky/content/content.db
```

That command executes each problem's own reference solution against its own test
cases, inside the same sandbox the grader uses, and marks a problem shippable
only if it agrees on every case. Problems that fail are kept with the reason they
failed rather than dropped, so a teacher can see the queue instead of finding out
that half the library quietly vanished.

Without this step the server has nothing to serve and will tell you so. The
upstream database itself is not redistributed here — see
[`docs/CONTENT_BUILD.md`](docs/CONTENT_BUILD.md) for the flags and
[`docs/CONTENT_VERIFICATION.md`](docs/CONTENT_VERIFICATION.md) for what the last
verification run found. In that run, 5,132 problems were imported and 2,603 were
verified shippable; the exact number moves as the pipeline and the source data
change, and the server prints the count for the database you actually built when
it starts.

## Issue access codes

Running code requires an access code. Browsing a problem does not. Codes are
issued per student rather than one per class, so a run can be attributed:

```bash
ducky access-codes c1 "Ada" "Bob" --class-name "Period 1"
```

This creates the class if it does not exist, then prints one code per student.
Hand them out individually — **anyone holding a code can run code on this
machine.**

## Run the server

```bash
ducky web --allow-lan          # reachable from the classroom LAN
ducky web --host 127.0.0.1    # this machine only
```

The server listens on port 8765 by default and opens a browser on start. It
prints the addresses students should use, and `/api/health` reports its status.

**`--allow-lan` is deliberate and not a formality.** The default bind is
`0.0.0.0`, which means every device on the network can reach a service that
executes code. Rather than print a warning nobody reads, the server *refuses* to
bind `0.0.0.0` unless you pass `--allow-lan`; that makes the exposure a decision
you typed instead of a default you inherited. If you only want the machine in
front of you, pass `--host 127.0.0.1` and no flag is required.

Other flags: `--port`, `--content`, `--state`, `--classroom`, `--no-browser`.
`--classroom` must point at the same database `ducky access-codes` wrote to, or
every code will be rejected for a reason that is not visible from the browser.

## What a student sees

| Route | What it does |
|---|---|
| `/` | Landing page and access-code sign-in |
| `/practice` | The problem list, filtered by topic, difficulty and search text, 50 per page |
| `/p/{slug}` | One problem: instructions, editor, test cases, results |
| `/api/health` | Server status, plus what the sandbox claims to be enforcing |

The sign-in exchanges an access code for a session cookie, so the code is typed
once. Drafts are saved server-side per problem. Grading is rate limited per
signed-in student, falling back to the client address for unauthenticated
requests — a per-address budget would be wrong here, because every student on a
classroom LAN shares one address. Host and Origin are checked, since a server on
`0.0.0.0` answers to any hostname and that is the shape a DNS-rebinding attack
needs.

## The security model, briefly

Containment is four independent layers, on the principle that no single barrier
is trustworthy and an escape from one should land inside the next:

1. A static AST allowlist that rejects dangerous imports, the escape builtins,
   and dunder attribute access outside a small allowlist.
2. POSIX rlimits — CPU, `fork()`, and file descriptors.
3. A wallclock timeout plus a process-group `SIGKILL` and a cap on captured
   output.
4. An environment built from scratch from an allowlist, so `GEMINI_API_KEY` and
   the real `~/.ducky` are not readable from inside a graded run.

The reference solution for each problem is stored in the content database but is
unreachable from the server: a read-only `ContentStore` installs a SQLite
authorizer that denies reads of the answer-key tables, so the guarantee does not
depend on a `WHERE` clause someone can forget.

`docs/SECURITY_MODEL.md` has the full model, the test for every claim in it, and
the list of what is not protected.

## Development

```bash
pip install -e ".[dev]"

python -m pytest                          # the suite
python tools/check_editor.py              # browser check; needs Chrome
.venv/bin/ruff check src tests tools      # lint
```

`tools/check_editor.py` is not optional decoration. CodeMirror 6 fails
*silently*: a bare import specifier missing from the import map produces a dead
editor with no console error, which looks fine in a screenshot. That check
asserts on rendered DOM in a real browser.

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request — in
particular the fail-first rule, which is the one that matters here.

## Documentation

Accurate for the current server:

* [`docs/SECURITY_MODEL.md`](docs/SECURITY_MODEL.md) — what the sandbox does and
  does not stop, with a test for every claim
* [`docs/CONTENT_BUILD.md`](docs/CONTENT_BUILD.md) — building the `content.db`
  the server serves
* [`docs/CONTENT_VERIFICATION.md`](docs/CONTENT_VERIFICATION.md) — what is wrong
  with the problem library, and how much of it can be graded

Known stale, and not yet updated: `docs/ARCHITECTURE.md`, `docs/API.md`,
`docs/DEVELOPMENT.md`, `docs/STUDENT_GUIDE.md` and `docs/TEACHER_GUIDE.md` still
describe the earlier teacher-server and lesson-pack design rather than this
practice server. Treat them as history until they are rewritten. The commands in
this README and in [CONTRIBUTING.md](CONTRIBUTING.md) are the ones verified
against the current code.

## Credits and licence

MIT — see [LICENSE](LICENSE). See [NOTICE](NOTICE) for third-party content,
vendored code, and upstream projects.

This project is built on the maintainer's own earlier work, all MIT licensed:

* **`Ducky`** — the engine: sandbox, test runner, diagnostics, CLI
* **`Python-practice-platform`** — the content pipeline and problem database
* **`gpts-school-of-python`** — exercise content authored for this project

**CodeMirror 6** (with its Lezer, `style-mod` and `w3c-keyname` dependencies) is
vendored under `static/vendor/cm/` as unmodified ES modules, under its own MIT
licence, copyright Marijn Haverbeke and others. Each vendored bundle ships with
its upstream `.LICENSE` file next to it.
