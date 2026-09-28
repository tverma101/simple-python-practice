"""Execution sandbox for untrusted student code.

This module owns every containment concern for grading a student submission, so
that ``runner.py`` stays readable and so the policy is stated in exactly one
place. The design follows the layered model described in
https://chs.us/2026/07/sandboxing-untrusted-python/ -- the premise being that no
single barrier is trustworthy and that an escape from one layer must land
inside the next.

Layers implemented here, and what each one actually buys:

===== ============================== ================================================
Layer Mechanism                      Question it answers
===== ============================== ================================================
1     AST allowlist (:func:`check_   "Can this code even *express* a dangerous
      student_code`)                 operation?" Static, so it rejects whole
                                     categories of attack before a single
                                     instruction runs.
2     rlimits (:func:`write_         "Can it exhaust CPU, memory, file
      bootstrap`)                    descriptors, or spawn helpers?"
3     wallclock timeout + process-   "Can it hang forever?" Two independent
      group SIGKILL                  timeouts, because either can be defeated.
4     environment scrub              "What can it read that it should not?"
      (:func:`build_sandbox_env`)
===== ============================== ================================================

Layers 5 and 6 from that model -- seccomp and Landlock -- are Linux kernel
features and are **not** implemented, because this project must also run on
macOS where they do not exist. On macOS the honest containment boundary is
layers 0-4: a dedicated low-privilege subprocess with rlimits and a wallclock.
Code that achieves native execution can still read whatever the invoking user
can read, minus the environment scrub in layer 4. If you need a stronger
boundary than that, run the whole grader inside nsjail or a container; see
``docs/SECURITY_MODEL.md``.

Two consequences worth stating plainly, because they are judgement calls rather
than oversights:

* The AST layer denies a list of *dangerous modules*, not all imports. A blanket
  import ban would be simpler, but it produces false rejections of legitimate
  teaching code, and a denylist students learn to route around is worse than an
  honest layered boundary. Layers 2-4 are what make the remaining surface
  survivable.
* ``input()`` is deliberately allowed. Lessons teach it, stdin is ``DEVNULL``
  during grading (``runner._run_command``) so it raises ``EOFError`` rather than
  hanging, and banning a builtin students are actively taught to use is a bad
  trade. See :data:`BLOCKED_NAMES` for which builtins are denied and why each
  one is there.
"""

from __future__ import annotations

import ast
import os
import stat
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "SandboxPolicy",
    "SandboxViolation",
    "DEFAULT_POLICY",
    "check_student_code",
    "build_sandbox_env",
    "write_bootstrap",
    "truncate_output",
    "SandboxLimits",
]


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxPolicy:
    """Tunable limits for a single graded run.

    Defaults are deliberately generous for a learner who is on their fifth
    attempt and deliberately stingy for a machine that is also running a web
    server. ``wall_seconds`` is the number a human actually feels, so it is the
    one most worth tuning per lesson.
    """

    wall_seconds: int = 15
    cpu_seconds: int = 10
    memory_bytes: int = 256 * 1024 * 1024
    max_open_files: int = 64
    max_processes: int = 0  # 0 => RLIMIT_NPROC 0, i.e. fork() is refused
    max_output_bytes: int = 256 * 1024
    #: Apply RLIMIT_AS. Off by default on macOS -- see _apply_as_limit.
    limit_address_space: bool | None = None

    def resolved_limit_address_space(self) -> bool:
        if self.limit_address_space is not None:
            return self.limit_address_space
        # RLIMIT_AS is unreliable for CPython on Darwin: the VM reserves address
        # space far beyond live heap, so a limit low enough to stop a memory bomb
        # also prevents the interpreter from starting. We take the weaker
        # guarantee there and say so, rather than shipping a limit that appears
        # to work and does not.
        return sys.platform != "darwin"

    def to_dict(self) -> dict[str, object]:
        return {
            "wall_seconds": self.wall_seconds,
            "cpu_seconds": self.cpu_seconds,
            "memory_bytes": self.memory_bytes,
            "max_open_files": self.max_open_files,
            "max_processes": self.max_processes,
            "max_output_bytes": self.max_output_bytes,
            "limit_address_space": self.resolved_limit_address_space(),
        }


DEFAULT_POLICY = SandboxPolicy()

#: Used for the interactive REPL, where there is no submission to grade and a
#: 15-second ceiling would make the console feel broken.
REPL_POLICY = SandboxPolicy(wall_seconds=10, cpu_seconds=8)


# ---------------------------------------------------------------------------
# Layer 1 -- static AST allowlist
# ---------------------------------------------------------------------------


class SandboxViolation(Exception):
    """Student code was rejected before execution.

    Carries a student-readable ``message`` and a machine-readable ``rule`` so
    that the diagnostics layer can turn it into a diagnosis instead of a stack
    trace.
    """

    def __init__(self, rule: str, message: str, *, line: int | None = None) -> None:
        self.rule = rule
        self.line = line
        location = f" (line {line})" if line else ""
        super().__init__(f"{message}{location}")
        self.message = message


#: Modules that hand the interpreter (or the kernel) to an attacker. This is a
#: denylist on purpose -- see the module docstring.
BLOCKED_MODULES = frozenset(
    {
        # Direct escape to the host
        "os", "sys", "subprocess", "posix", "nt", "_posixsubprocess",
        "ctypes", "_ctypes", "multiprocessing", "_thread", "threading",
        "importlib", "imp", "builtins", "__builtin__", "pkgutil", "runpy",
        "code", "codeop", "pdb", "bdb", "trace", "sysconfig",
        # Filesystem and process reach
        "shutil", "pathlib", "tempfile", "fileinput", "glob", "resource",
        "signal", "pty", "fcntl", "termios", "tty", "at_exit", "atexit",
        "gc", "inspect", "dis", "sys.audit",
        # Network
        "socket", "ssl", "select", "selectors", "asyncio", "http", "urllib",
        "urllib2", "urllib3", "ftplib", "smtplib", "poplib", "imaplib",
        "nntplib", "telnetlib", "smtpd", "webbrowser", "xmlrpc", "socketserver",
        # Serialization / code generation
        "pickle", "cPickle", "marshal", "shelve", "base64", "codecs",
        "unicodedata", "mmap", "array",
        # Credential and host snooping
        "getpass", "pwd", "grp", "crypt", "platform", "keyring",
        "faulthandler", "cProfile", "pstats", "timeit",
    }
)

#: Builtins that are either an escape primitive or a way to reach one. Note that
#: ``input``, ``open``, ``getattr``, ``globals``, ``vars`` and ``dir`` are
#: deliberately absent: they are taught, and the env scrub plus rlimits already
#: bound what they can reach.
#: Builtins a submission may not name.
#:
#: Every entry here was added because it was demonstrated to hand data back to
#: the student, not as defence in depth. The grader returns the submission's
#: actual value in the response, so a builtin that can read a file is a file
#: exfiltration primitive with the student as the attacker and the server as the
#: host:
#:
#:   open("/etc/passwd").read()      -> the file's contents, in ``actual_repr``
#:   globals()                        -> the module namespace, likewise
#:   getattr(a, "__cla" + "ss__")    -> any attribute of any object
#:
#: The last one is why ``getattr`` is here rather than a longer attribute
#: denylist. :data:`ALLOWED_DUNDERS` is enforced only for *literal* attribute
#: names, so ``x.__class__`` is rejected and ``getattr(x, "__cla" + "ss__")`` is
#: not. The ladder ``__class__`` -> ``__mro__`` -> ``__subclasses__`` reaches
#: every class in the interpreter from a harmless string. Blocking ``getattr``
#: is what makes the attribute allowlist mean anything.
#:
#: Deliberately *not* blocked, each checked against the content rather than
#: assumed:
#:
#: ``input``  The module docstring argues it is safe because grading runs with
#:            ``stdin=DEVNULL`` (``runner._run_command``), so it raises
#:            ``EOFError`` immediately instead of blocking. That holds, and no
#:            verified problem uses it, but it is taught, so blocking it would
#:            buy nothing.
#: ``dir``    Returns strings only. Once ``getattr`` is blocked it cannot reach
#:            an attribute, so it is a string lister and nothing more.
#: ``hasattr`` Reveals one boolean. Same reasoning.
#: ``id``     Returns an integer that means nothing across the boundary.
#: ``print``  These exercises compare stdout; suppressing it would break them.
#:
#: None of the 2,603 verified problems use ``open``, ``getattr``, ``setattr``,
#: ``delattr``, ``globals``, ``locals`` or ``vars``, in their starter code or
#: their text, so this list costs the current curriculum nothing.
BLOCKED_NAMES = frozenset(
    {
        "exec", "eval", "compile", "__import__", "breakpoint",
        "memoryview", "exit", "quit",
        # Filesystem.
        "open", "file",
        # Attribute access by name, which is what defeats a dunder allowlist.
        "getattr", "setattr", "delattr",
        # Namespace exfiltration.
        "globals", "locals", "vars",
        # The dunder escape hatch back to the real builtins.
        "__builtins__", "__loader__", "__spec__",
    }
)

#: Dunder attributes a submission may touch. Class authoring legitimately needs
#: ``__init__``/``__str__``/``__eq__``; the object-introspection ladder
#: (``__class__`` -> ``__bases__`` -> ``__subclasses__``) is what must not be
#: reachable. This is an allowlist rather than a denylist because the denylist
#: version has a long tail of near-misses.
ALLOWED_DUNDERS = frozenset(
    {
        "__init__", "__new__", "__str__", "__repr__", "__eq__", "__ne__",
        "__lt__", "__le__", "__gt__", "__ge__", "__hash__", "__len__",
        "__iter__", "__next__", "__contains__", "__getitem__", "__setitem__",
        "__delitem__", "__call__", "__add__", "__radd__", "__sub__",
        "__rsub__", "__mul__", "__rmul__", "__truediv__", "__floordiv__",
        "__mod__", "__pow__", "__neg__", "__abs__", "__and__", "__or__",
        "__xor__", "__invert__", "__lshift__", "__rshift__", "__int__",
        "__float__", "__bool__", "__index__", "__enter__", "__exit__",
        "__name__", "__doc__", "__module__", "__qualname__", "__version__",
        "__all__", "__slots__", "__annotations__", "__dict__",
        "__format__", "__repr_html__", "__fspath__", "__aenter__", "__aexit__", "__aiter__", "__anext__",
        "__post_init__",
    }
)

#: Reflection primitives that perform attribute access driven by a runtime
#: value. A static AST check cannot see through these -- ``getattr(x, name)``
#: looks identical whether ``name`` is a literal or attacker-controlled. They
#: are allowed because they are taught, and this list exists so the limitation
#: is documented rather than forgotten. Layers 2-4 are the mitigation.
DATA_DRIVEN_REFLECTION = ("getattr", "format", "format_map", "__getattr__")


@dataclass
class _ASTFindings:
    violations: list[SandboxViolation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _root_module(name: str) -> str:
    return name.split(".", 1)[0]


def check_student_code(source: str, *, policy: SandboxPolicy = DEFAULT_POLICY) -> None:
    """Reject student code that statically expresses a dangerous operation.

    Raises :class:`SandboxViolation` on the first category of violation found.
    Passing does **not** mean the code is safe -- it means the code did not
    reach for the obvious handles. Layers 2-4 cover the rest.

    A :class:`SyntaxError` propagates unchanged and is *not* a sandbox violation:
    unparseable code is the student's problem to fix, and the grading run reports
    it far better than a policy message would.

    This function is a *pre-filter*. It is intentionally cheap and total: it
    either returns or raises, and it never executes anything.
    """
    # Deliberately not wrapped: a SyntaxError must reach the caller intact.
    tree = ast.parse(source)

    findings = _ASTFindings()
    _walk_for_violations(tree, findings)

    if findings.violations:
        raise findings.violations[0]


def _walk_for_violations(tree: ast.AST, findings: _ASTFindings) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = _root_module(alias.name)
                if root in BLOCKED_MODULES:
                    findings.violations.append(
                        SandboxViolation(
                            "blocked_import",
                            f"Importing '{alias.name}' is not allowed in this exercise.",
                            line=node.lineno,
                        )
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                findings.violations.append(
                    SandboxViolation(
                        "blocked_import",
                        "Relative imports are not allowed in this exercise.",
                        line=node.lineno,
                    )
                )
            elif node.module:
                root = _root_module(node.module)
                if root in BLOCKED_MODULES:
                    findings.violations.append(
                        SandboxViolation(
                            "blocked_import",
                            f"Importing from '{node.module}' is not allowed in this exercise.",
                            line=node.lineno,
                        )
                    )
        elif isinstance(node, ast.Attribute):
            attr = node.attr
            if attr.startswith("__") and attr.endswith("__") and attr not in ALLOWED_DUNDERS:
                findings.violations.append(
                    SandboxViolation(
                        "blocked_attribute",
                        f"Accessing '{attr}' is not allowed in this exercise.",
                        line=node.lineno,
                    )
                )
        elif isinstance(node, ast.Name):
            if node.id in BLOCKED_NAMES:
                findings.violations.append(
                    SandboxViolation(
                        "blocked_builtin",
                        f"'{node.id}' is not available in this exercise.",
                        line=node.lineno,
                    )
                )
        elif isinstance(node, ast.Global) or isinstance(node, ast.Nonlocal):
            # Harmless on its own, but a common half of an escape that pairs
            # `global` with a rebound builtin. Left allowed: comprehensions and
            # some teaching examples legitimately use `global`.
            pass


def describe_policy(policy: SandboxPolicy = DEFAULT_POLICY) -> str:
    """Human-readable policy summary, for `ducky sandbox explain` and docs."""
    limits = policy.to_dict()
    rows = [
        f"wall clock       {limits['wall_seconds']}s",
        f"cpu time         {limits['cpu_seconds']}s",
        f"address space    {limits['memory_bytes'] // (1024 * 1024)} MB"
        if limits["limit_address_space"]
        else "address space    not enforced on this platform",
        f"open files       {limits['max_open_files']}",
        f"child processes  {limits['max_processes']}",
        f"captured output  {limits['max_output_bytes'] // 1024} KB",
    ]
    return "sandbox policy\n  " + "\n  ".join(rows)


# ---------------------------------------------------------------------------
# Layer 2 -- rlimits, applied in the child via a bootstrap
# ---------------------------------------------------------------------------


_BOOTSTRAP_TEMPLATE = '''\
"""Generated by ducky.sandbox. Sets rlimits in the child, then execs the target.

This exists instead of ``subprocess.Popen(preexec_fn=...)`` because
``preexec_fn`` is documented as unsafe in the presence of threads, and the web
server this project grows is threaded. A bootstrap script is thread-safe: the
limits are set in a freshly exec'd process that has nothing else running in it.

It also records which limits it *actually* achieved into a sidecar file. A
limit the kernel refuses is a limit we do not have, and the parent is the only
place that can tell the student (and the docs) the truth about it.
"""

import json
import os
import resource
import sys

_LIMITS = {limits!r}
_REPORT_PATH = {report_path!r}

_applied = {{}}


def _set(name, which, soft):
    try:
        resource.setrlimit(which, (soft, soft))
    except (ValueError, OSError) as exc:
        # A limit we cannot lower is a limit we do not have. Failing loudly here
        # would abort grading of an otherwise fine submission, so we record the
        # refusal and let the wallclock layer carry the load.
        _applied[name] = False
        return
    try:
        actual = resource.getrlimit(which)[0]
    except (ValueError, OSError):
        actual = None
    _applied[name] = actual == soft


_set("cpu_seconds", resource.RLIMIT_CPU, _LIMITS["cpu_seconds"])
_set("max_open_files", resource.RLIMIT_NOFILE, _LIMITS["max_open_files"])
if _LIMITS["max_processes"] == 0:
    # Refusing fork() matters even though we have no escape to make use of it:
    # a helper process is a way to outlive the timeout.
    _set("max_processes", resource.RLIMIT_NPROC, 0)
if _LIMITS["limit_address_space"]:
    _set("memory_bytes", resource.RLIMIT_AS, _LIMITS["memory_bytes"])

try:
    with open(_REPORT_PATH, "w", encoding="utf-8") as handle:
        json.dump(_applied, handle)
except OSError:
    pass

os.execv(sys.argv[1], sys.argv[1:])
'''

APPLIED_LIMITS_FILENAME = "_ducky_applied_limits.json"


def write_bootstrap(directory: Path, policy: SandboxPolicy = DEFAULT_POLICY) -> Path:
    """Write the rlimit bootstrap into ``directory`` and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    report_path = directory / APPLIED_LIMITS_FILENAME
    # Clear a stale report so a crashed run cannot make the next run look
    # protected when it was not.
    try:
        report_path.unlink()
    except OSError:
        pass
    path = directory / "_ducky_sandbox_exec.py"
    path.write_text(
        _BOOTSTRAP_TEMPLATE.format(
            limits=policy.to_dict(),
            report_path=str(report_path),
        ),
        encoding="utf-8",
    )
    # Never executable: it is only ever handed to the interpreter.
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def read_applied_limits(directory: Path) -> dict[str, bool]:
    """Read the sidecar the bootstrap wrote: which limits it truly achieved.

    Empty dict means the child never got far enough to report, in which case the
    caller must assume it had nothing.
    """
    import json as _json

    try:
        raw = (directory / APPLIED_LIMITS_FILENAME).read_text(encoding="utf-8")
        data = _json.loads(raw)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): bool(v) for k, v in data.items()}


def rlimits_supported() -> bool:
    """``resource`` is POSIX-only. Windows gets the wallclock layer alone."""
    try:
        import resource  # noqa: F401
    except ImportError:
        return False
    return os.name == "posix"


# ---------------------------------------------------------------------------
# Layer 4 -- environment scrub
# ---------------------------------------------------------------------------


#: Variables the child genuinely needs. Everything else in the parent's
#: environment -- API keys, cloud credentials, tokens, Ducky config -- is
#: withheld. This is the layer that stops student code from reading
#: ``~/.ducky/config.json`` or printing ``GEMINI_API_KEY`` into a test report.
PASSTHROUGH_ENV = (
    "SYSTEMROOT",  # Windows, harmless elsewhere
    "COMSPEC",
    "PATHEXT",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)


def build_sandbox_env(
    work_dir: Path,
    *,
    scratch_dir: Path | None = None,
    extra: dict[str, str] | None = None,
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a from-scratch environment for a graded subprocess.

    Args:
        work_dir: The working directory for this run. It becomes ``PYTHONPATH``
            so the child imports the submission under test.
        scratch_dir: Where the child's ``HOME`` and ``TMPDIR`` point. Defaults to
            ``work_dir``. Pass a separate directory when grading in place -- a
            teacher running a lesson from its own folder should not acquire
            ``home/`` and ``tmp/`` subdirectories in it, and the test-tamper
            snapshot would rightly notice the new files.
        extra: Explicit additions. The only supported way to widen the
            environment; there is no "inherit everything" switch, on purpose.
        base: Read the platform essentials from here instead of
            ``os.environ``. Defaults to ``os.environ``; tests pass a clean dict.
    """
    source: dict[str, str] = os.environ if base is None else base
    scratch = work_dir if scratch_dir is None else scratch_dir

    env: dict[str, str] = {}

    for name in PASSTHROUGH_ENV:
        value = source.get(name)
        if value:
            env[name] = value

    # A minimal PATH. The test command is invoked as
    # ``sys.executable -m pytest``, an absolute path, so PATH is not load-bearing
    # -- but some tooling shells out, and an empty PATH produces confusing
    # errors rather than clean ones.
    path = source.get("PATH", "")
    env["PATH"] = path if path else os.defpath

    # Redirect the writable, readable world the child sees into its own sandbox
    # directory. This is what actually removes access to ~/.ducky.
    home = scratch / "home"
    tmp = scratch / "tmp"
    for target in (home, tmp):
        target.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["TMPDIR"] = str(tmp)
    env["TEMP"] = str(tmp)
    env["TMP"] = str(tmp)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_CACHE_HOME"] = str(home / ".cache")
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")

    # Import and packaging hygiene
    env["PYTHONPATH"] = str(work_dir)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Do not let a stray sitecustomize or usercustomize inject code.
    env["PYTHONNOUSERSITE"] = "1"

    # Deterministic, encoding-stable grading
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # Fixed hash seed so that set/dict iteration order -- and therefore printed
    # output -- is reproducible between a student's run and a later re-grade.
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONWARNINGS"] = "ignore"

    # Locale: a fixed, non-macOS-default locale stops number and date
    # formatting from silently changing between machines.
    env["LANG"] = "C.UTF-8"
    env["LC_ALL"] = "C.UTF-8"

    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


# ---------------------------------------------------------------------------
# Output capping
# ---------------------------------------------------------------------------


def truncate_output(text: str, limit: int) -> tuple[str, bool]:
    """Clamp ``text`` to ``limit`` characters.

    Returns ``(text, was_truncated)``. Truncation is a *reporting* concern --
    the process is killed separately by the reader loop -- so the marker is
    written into the text so the student can see that output was cut off rather
    than wondering why their last line vanished.
    """
    if limit <= 0 or len(text) <= limit:
        return text, False
    marker = f"\n... [output truncated at {limit} bytes] ...\n"
    return text[: max(0, limit - len(marker))] + marker, True


@dataclass
class SandboxLimits:
    """Resolved, platform-aware limits actually applied to a run."""

    policy: SandboxPolicy = DEFAULT_POLICY
    rlimits_applied: bool = False
    address_space_enforced: bool = False
    network_isolated: bool = False
    #: Ground truth from the bootstrap sidecar, when available. Empty means
    #: "not observed yet", which is not the same as "not enforced".
    applied: dict[str, bool] = field(default_factory=dict)

    def enforcement_summary(self) -> str:
        """Honest one-liner about what this run was and was not protected by.

        When ``applied`` is populated it comes from the bootstrap sidecar, i.e.
        from what the kernel actually did, not from what we asked for. That
        distinction is the whole point: a refused limit must never be reported
        as a working one.
        """
        bits = ["wallclock", "ast-filter", "env-scrubbed"]
        applied = self.applied
        if applied:
            if applied.get("cpu_seconds"):
                bits.append("cpu-limit")
            if applied.get("max_processes"):
                bits.append("no-fork")
            if applied.get("memory_bytes"):
                bits.append("memory-limit")
            else:
                bits.append("NO-memory-limit")
            if not applied.get("max_open_files"):
                bits.append("NO-fd-limit")
        else:
            if self.rlimits_applied:
                bits.append("cpu-limit")
                bits.append("no-fork")
            bits.append(
                "memory-limit" if self.address_space_enforced else "NO-memory-limit"
            )
        bits.append("network-ISOLATED" if self.network_isolated else "network-allowed")
        return "+".join(bits)

    def weakest_layer(self) -> str:
        """The one layer a determined submission would get through.

        Surfaced so ``docs/SECURITY_MODEL.md`` and the UI can state the boundary
        rather than leaving a reader to assume it.
        """
        if not self.rlimits_applied:
            return "wallclock only"
        if self.applied and not self.applied.get("memory_bytes"):
            return "resource exhaustion (memory); the wallclock is the only backstop"
        if not self.network_isolated:
            return "network access; the sandbox is a speed bump, not a jail"
        return "kernel-level isolation (not implemented on this platform)"


def resolve_limits(
    policy: SandboxPolicy = DEFAULT_POLICY,
    *,
    applied: dict[str, bool] | None = None,
) -> SandboxLimits:
    """Work out which layers this platform can actually enforce.

    Pass ``applied`` (from :func:`read_applied_limits`) once a run has happened
    to replace assumption with observation.
    """
    rlimits = rlimits_supported()
    return SandboxLimits(
        policy=policy,
        rlimits_applied=rlimits,
        address_space_enforced=(
            bool(applied.get("memory_bytes"))
            if applied
            else (rlimits and policy.resolved_limit_address_space())
        ),
        # Always False: we have no portable way to cut a child process off from
        # the network on macOS, and claiming otherwise would be worse than
        # saying so.
        network_isolated=False,
        applied=dict(applied) if applied else {},
    )


def scrubbed_env_keys(base: dict[str, str] | None = None) -> Iterable[str]:
    """Keys present in ``base`` that :func:`build_sandbox_env` would withhold.

    Exposed so the test suite can assert the scrub is actually doing something,
    and so `ducky sandbox explain` can show a user what was withheld.
    """
    source: dict[str, str] = os.environ if base is None else base
    allowed = set(PASSTHROUGH_ENV) | {"PATH"}
    for key in sorted(source):
        if key not in allowed:
            yield key
