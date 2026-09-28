# Security Policy

## Reporting a vulnerability

**Report it privately.** Do not open a public issue.

Use GitHub's private vulnerability reporting on this repository:

<https://github.com/tverma101/simple-python-practice/security/advisories/new>

That opens a private advisory visible only to you and the maintainer. It is the
only channel for anything security-sensitive.

> **If that link 404s:** private vulnerability reporting is not enabled yet. The
> maintainer turns it on under *Settings → Code security → Private vulnerability
> reporting*. Until then, open an issue whose **entire contents** are one line
> asking for a private contact channel, with no technical detail, no proof of
> concept, and no reproduction. That is the only situation in which a public
> issue is the right move, and it is still a poor substitute — enable private
> reporting instead.

`docs/SECURITY_MODEL.md` carries the full model and defers here for reporting.

### Do not file a public issue for a sandbox escape

A public issue is a working exploit with a reproduction attached, published to
everyone, indexed within minutes, and notified to exactly the people best placed
to use it. If you can get out of the sandbox, that is a defect in this project's
security claims and the maintainer needs to hear about it before anyone else
does — which is the opposite of what a public issue does.

This applies to:

* escaping the grading sandbox,
* reaching files, environment variables, or the answer key that a layer is
  supposed to deny,
* defeating the access-code check, the rate limit, or the Host/Origin guard,
* anything that lets a signed-in student read another student's data, or an
  unauthenticated visitor run code.

If you are unsure which bucket something falls into, use the private advisory
channel. Nobody is penalised for a report that turns out not to be a bug.

## What to include

A report that reproduces is worth ten that do not. Useful:

* the submission that got through, verbatim, and what you expected to happen;
* how you reached it — the access-code state, the URL, the request;
* the output you got, especially the value the grader returned to the browser;
* your platform and Python version (`python -VV`), because the sandbox enforces
  different things on macOS and Linux.

Do not include real API keys, real student data, or anything from a machine you
do not own. Redact before you paste.

## Threat model, in one paragraph

The thing being defended is one user's machine, from one learner's Python
submission, while that submission is being graded. The threat model is a student
who is curious — someone exploring what they can reach — not an attacker with
resources. Containment is layered (static AST allowlist, POSIX rlimits, wallclock
and process-group kill, environment scrub), and every claim about it is backed by
a test.

It is **not** a model for a public multi-tenant service. There is no uid drop, no
seccomp, no Landlock, and no network isolation; on macOS, memory is bounded only
by the wallclock timeout, because `RLIMIT_AS` has a hard ceiling there that is
too low to let CPython start. The sandbox does not pretend otherwise: the
bootstrap reports what the kernel actually did, `enforcement_summary()` names
which limits were refused, and `weakest_layer()` names the boundary at runtime.

**[`docs/SECURITY_MODEL.md`](docs/SECURITY_MODEL.md) is the full model** — what
each layer stops, where it lives in the code, and a section on what is not
protected. Read it before deploying this anywhere. The short version is that if
you need a real boundary, put the whole grader inside a container or nsjail and
treat this as the inner application layer.

## Supported versions

This is a small project with one line of development and no release tags. Fixes
land on `main` and there are no backports. If you are running a fork, backport
the fix yourself.

## Disclosure

The maintainer will acknowledge a report, and will credit you in the advisory
unless you would rather stay anonymous. There is no formal timeline; reports that
turn out to be real escapes get priority over everything else on the list.
