# Security model

This document exists because the most useful thing a learning platform can say
about untrusted code is exactly what it does and does not protect you from. It
is written to be checkable: every claim here has a test in
`tests/test_sandbox.py`, and where a protection is *absent* that is stated
rather than left for a reader to assume.

## Scope

The thing being defended is a single user's machine, from a single learner's
Python submission, while that submission is being graded. The threat model is a
student who is curious, not a student who is attacking: someone exploring what
they can reach. It is **not** a model for a public multi-tenant service. If you
deploy this where strangers can submit code, this model does not hold and you
need the container boundary described in [What this is not](#what-this-is-not).

## The layers

Student code runs in a dedicated subprocess. Containment comes from four
independent layers, following the model in
<https://chs.us/2026/07/sandboxing-untrusted-python/>. The premise is that no
single barrier is trustworthy and an escape from one should land inside the next.

| # | Layer | What it stops | Where it lives |
|---|-------|---------------|----------------|
| 1 | Static AST allowlist | Whole *categories* of attack, before a single instruction runs | `sandbox.check_student_code` |
| 2 | POSIX rlimits | CPU exhaustion, `fork()`, file-descriptor exhaustion | `sandbox.write_bootstrap` |
| 3 | Wallclock + process-group `SIGKILL` | Hangs, and helpers that outlive the timeout | `runner._run_command` |
| 4 | Environment scrub | Reading API keys, cloud credentials, and `~/.ducky` | `sandbox.build_sandbox_env` |

### Layer 1 — static AST allowlist

Rejects `import` of a denylist of dangerous modules, the escape builtins
(`exec`, `eval`, `compile`, `__import__`, `open`, `getattr`, `setattr`,
`delattr`, `globals`, `locals`, `vars`), and dunder attribute access outside a
small allowlist. The dunder rule is the important one: `().__class__.__bases__[0].__subclasses__()`
is a full escape with no import statement at all, so blocking imports alone
stops nothing against a determined attacker.

`getattr` is blocked for a specific reason rather than as blanket caution. The
dunder allowlist is enforced only against **literal** attribute names, so
`x.__class__` is rejected while `getattr(x, "__cla" + "ss__")` is not — verified
against the running server, where the second form returned `<class 'int'>`.
Blocking `getattr` is what makes the allowlist mean anything.

`open` is blocked because the grader returns the submission's own value to the
student's browser. That turns any readable file into an exfiltration
primitive, and it was not theoretical: `open("/etc/passwd").read()` returned the
file's contents in the response's `actual_repr` before this was fixed.

It is a **pre-filter, not a boundary**. Passing means the code did not reach for
the obvious handles, nothing more.

It is a denylist of dangerous modules rather than a ban on all imports, on
purpose. An earlier version of the *other* codebase in this project used a
substring blacklist and rejected `dir()` — a builtin that is taught in lesson
one — which trains students to route around filters and gets the filter switched
off. Layers 2–4 are what make the residual surface survivable. The legitimate
code that must not be rejected is pinned by
`TestStaticFilterAllows`, so loosening the filter is a visible test failure
rather than a silent hole.

Every builtin in `BLOCKED_NAMES` is there because it was demonstrated to hand
data back to a student, and the ones left out were checked rather than assumed.
`input` stays allowed because grading runs with `stdin=DEVNULL`
(`runner._run_command`), so it raises `EOFError` immediately instead of holding
a grader slot. `dir`, `hasattr` and `id` return strings, booleans and integers
and, once `getattr` is blocked, cannot reach an attribute. None of the 2,603
verified problems use `open`, `getattr`, `setattr`, `delattr`, `globals`,
`locals` or `vars` in their starter code or their text, so the list costs the
current curriculum nothing.

### Layer 2 — rlimits

Applied in the child by a generated bootstrap script, not via
`subprocess.Popen(preexec_fn=...)`, because `preexec_fn` is documented as unsafe
in the presence of threads and the web server is threaded.

* `RLIMIT_CPU` — set below the wallclock, deliberately, so a runaway loop
  produces "you ran out of CPU" rather than a vague "timed out".
* `RLIMIT_NPROC = 0` — refuses `fork()`. A helper process is a way to outlive the
  timeout even when the parent is killed.
* `RLIMIT_NOFILE` — bounds descriptor exhaustion.

### Layer 3 — wallclock and output cap

Two independent timeouts, because either can be defeated. On timeout the whole
process *group* is `SIGKILL`ed (`start_new_session=True` plus `os.killpg`), so
a child that spawned grandchildren does not leave them running.

Captured output is capped and the group is killed the moment the cap is passed.
Streams are drained incrementally rather than via `communicate()`, which buffers
without bound — before this change `while True: print(...)` was bounded only by
the wallclock, i.e. up to 15 seconds of unbounded growth in the grader per
submission.

### Layer 4 — environment scrub

The child environment is built from scratch from an allowlist. It does **not**
call `os.environ.copy()`.

This closed a live bug. Student code previously inherited the entire parent
environment, so `GEMINI_API_KEY` and `OPENAI_API_KEY` were readable from inside a
graded run, as was `~/.ducky/config.json`. The child's `HOME`, `TMPDIR` and the
`XDG_*` paths are redirected into the sandbox directory, which is what puts the
real `~/.ducky` out of reach. `TestEnvironmentScrub::test_cannot_read_the_real_ducky_home`
is the regression test.

Also fixed here for reproducibility: `PYTHONHASHSEED=0` (so set/dict iteration
order cannot change printed output between a run and a re-grade) and
`LC_ALL=C.UTF-8` (so number and date formatting cannot change between machines).

## What is NOT protected

These are the honest gaps. Each is a place where a determined submission gets
through.

### Memory exhaustion is not bounded on macOS

`RLIMIT_AS` is unreliable for CPython on Darwin: the VM reserves address space
far beyond live heap, and macOS enforces a hard ceiling on `RLIMIT_AS` well
below 2 GB, so a limit low enough to stop a memory bomb also prevents the
interpreter from starting. Measured on this machine, `setrlimit(RLIMIT_AS, 2GB)`
raises `ValueError: current limit exceeds maximum limit`.

So on macOS **a memory bomb is bounded only by the wallclock**. A submission
doing `x = [0] * 10**10` will allocate for the full window before being killed.

The policy does not paper over this. `limit_address_space` resolves to `False` on
Darwin, the bootstrap records `memory_bytes: false` in a sidecar, and
`enforcement_summary()` reports `NO-memory-limit`. The bootstrap's sidecar is
written from what the kernel *actually did*, not from what we asked for, and
`TestHonestReporting::test_refused_limit_is_reported_as_refused` exists purely to
stop anyone reintroducing a claim we have not earned.

On Linux, set `limit_address_space=True` and the limit applies.

### There is no network isolation

Student code can reach the network if the machine has one. There is no portable
way to cut a child process off from the network on macOS, so
`network_isolated` is hard-coded `False` and the summary says
`network-allowed`. On Linux, seccomp and Landlock (layers 5 and 6 of the
referenced model) would close this; neither is implemented here.

### No uid drop, and no seccomp or Landlock

Layers 0, 5 and 6 of the referenced model are unimplemented. Code that reaches
native execution can read whatever the invoking user can read, minus the layer-4
scrub. `weakest_layer()` names this boundary at runtime rather than leaving a
reader to infer it.

### Data-driven reflection is not statically analysable

`str.format`, `%`-formatting, and `operator.attrgetter` all perform attribute
access driven by a *runtime* value, so a static AST allowlist cannot see through
them. This remains a real limitation of layer 1, recorded in
`DATA_DRIVEN_REFLECTION` so it is a documented gap rather than a forgotten one.
Layers 2–4 are the mitigation.

`getattr` used to sit in this list as an allowed gap. It no longer does, for the
reason given under layer 1: with the function available, a computed name reaches
any attribute and the dunder allowlist only covers literal ones. What remains is
that formatting an object can still ask it to read its own attributes; it cannot
be used to read a *file* or to reach a class the allowlist blocks.

### The tests are readable by the student

A student who can read `tests/` can reverse-engineer expected answers. The
tamper check (`test_tampering`) stops accidental and trivial mutation, not a
determined reader. This is inherent to running pytest against visible tests,
and is called out in the header of `tests/test_runner_adversarial.py`.

## What this is not

This is **not** a multi-tenant judge. Do not expose it to untrusted strangers.

If you need that, put the whole grader inside a container or nsjail and treat
this as the inner application layer. The snekbox nsjail configuration
(<https://github.com/python-discord/snekbox>) is a good reference; Judge0 is the
usual off-the-shelf answer. Both need privileges this project does not assume,
which is precisely why they are not the default here.

## Reporting a sandbox problem

**Report it privately. Do not open a public issue.**

A public issue with a working sandbox escape attached is a published exploit
with a reproduction, indexed within minutes and sent to exactly the people best
placed to use it. If you can get out of the sandbox, that is a bug in this
document's claims, and the maintainer needs to hear it before anyone else does.

Use the private channel in [`SECURITY.md`](../SECURITY.md) — GitHub's private
vulnerability reporting on this repository. Attach the submission that got
through there, along with what you expected to happen instead, and say which
layer you think it got past. That channel is the only one that keeps the escape
unpublished while it is fixed. The test the maintainer will extend is in
`tests/test_sandbox.py`: `TestStaticFilterAllows` or
`TestStaticFilterRejects`, depending on which side of the line it falls.

The rest of this document is accurate about what is *not* protected, and a
report that reaches a layer those sections already concede does not need to
arrive as a public proof of concept either. Use the private advisory for
anything security-sensitive, including the cases where you are not sure which
bucket it falls into.
