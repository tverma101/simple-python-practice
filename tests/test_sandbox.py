"""Tests for the execution sandbox.

Two kinds of assertion live here and the distinction matters:

* **Capability tests** -- "does this layer actually stop the attack?" These run
  real subprocesses and are allowed to be slow. A sandbox test that does not
  spawn anything is not testing a sandbox.
* **Contract tests** -- "does the policy report itself honestly?" These are fast
  and guard the failure mode that matters most here: claiming protection we do
  not have. A limit the kernel refused must never be reported as active.

The AST layer is a *pre-filter*, not a boundary. Several tests below assert
that it lets dangerous-looking-but-legitimate code through, because a filter
that cries wolf gets disabled and then it protects nothing.
"""

from __future__ import annotations

import os
import resource
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from ducky import sandbox
from ducky.sandbox import (
    DEFAULT_POLICY,
    SandboxPolicy,
    SandboxViolation,
    build_sandbox_env,
    check_student_code,
    read_applied_limits,
    resolve_limits,
    truncate_output,
    write_bootstrap,
)

# ── Layer 1: static AST allowlist ─────────────────────────────────────


class TestStaticFilterRejects:
    @pytest.mark.parametrize(
        ("source", "rule"),
        [
            ("import os", "blocked_import"),
            ("import sys", "blocked_import"),
            ("import subprocess", "blocked_import"),
            ("import socket", "blocked_import"),
            ("import ctypes", "blocked_import"),
            ("import pathlib", "blocked_import"),
            ("import shutil", "blocked_import"),
            ("from os import path", "blocked_import"),
            ("from subprocess import run", "blocked_import"),
            ("import os.path", "blocked_import"),
            ("from . import sibling", "blocked_import"),
            ("eval('1+1')", "blocked_builtin"),
            ("exec('x=1')", "blocked_builtin"),
            ("compile('x=1', '<s>', 'exec')", "blocked_builtin"),
            ("__import__('os')", "blocked_builtin"),
            ("().__class__.__bases__[0].__subclasses__()", "blocked_attribute"),
            ("x = ().__class__", "blocked_attribute"),
            ("f.__globals__['__builtins__']", "blocked_attribute"),
            ("f.__code__.co_consts", "blocked_attribute"),
            # Exfiltration, not defence in depth. The grader returns the
            # student's actual value, so each of these hands a file, the module
            # namespace or the type ladder straight back in the response.
            # Exfiltration, not defence in depth. The grader returns the
            # submission's actual value, so each of these hands a file or the
            # module namespace straight back in the response. Verified against
            # the running server: open("/etc/passwd").read() returned the file.
            ("open('/etc/passwd').read()", "blocked_builtin"),
            ("open('/etc/hosts').read()", "blocked_builtin"),
            ("open('f.txt', 'w')", "blocked_builtin"),
            ("globals()", "blocked_builtin"),
            ("locals()", "blocked_builtin"),
            ("vars()", "blocked_builtin"),
            # A computed name is what makes ALLOWED_DUNDERS meaningful: it is
            # enforced only for literal attribute names, so getattr is the
            # bypass and has to be blocked itself. Verified: this reached
            # <class 'int'> while a literal x.__class__ was rejected.
            ("getattr(a, '__cla' + 'ss__')", "blocked_builtin"),
            ("getattr(a, '__mro__')", "blocked_builtin"),
            ("setattr(a, 'x', 1)", "blocked_builtin"),
            ("delattr(a, 'x')", "blocked_builtin"),
        ],
    )
    def test_rejects(self, source, rule):
        with pytest.raises(SandboxViolation) as excinfo:
            check_student_code(source)
        assert excinfo.value.rule == rule

    def test_violation_carries_line_number(self):
        with pytest.raises(SandboxViolation) as excinfo:
            check_student_code("x = 1\ny = 2\nimport os\n")
        assert excinfo.value.line == 3

    def test_violation_message_is_student_readable(self):
        """A student should be told what happened, not shown a rule name."""
        with pytest.raises(SandboxViolation) as excinfo:
            check_student_code("import os")
        assert "not allowed" in excinfo.value.message
        assert "os" in excinfo.value.message
        assert "blocked_import" not in excinfo.value.message

    def test_syntax_error_is_not_a_sandbox_violation(self):
        """Unparseable code must reach the grader as a SyntaxError.

        Wrapping it in a policy exception would replace a useful message
        ("expected '(' at line 1") with a confusing one.
        """
        with pytest.raises(SyntaxError):
            check_student_code("def broken(")


class TestStaticFilterAllows:
    """Code that is legitimate in a teaching context must not be rejected.

    Each case here is something a Python course actually assigns. A filter that
    rejects these gets switched off, and a switched-off filter protects nothing.
    """

    @pytest.mark.parametrize(
        "source",
        [
            "print('hello')",
            "x = 1 + 2\nprint(x)",
            "for i in range(10):\n    print(i)",
            "d = {'a': 1}\nfor k, v in d.items():\n    print(k, v)",
            "xs = [i * i for i in range(10)]",
            "{k: v for k, v in [('a', 1)]}",
            "{v for v in [1, 2, 3]}",
            "def add(a, b=0, *args, **kwargs):\n    return a + b",
            "def outer():\n    def inner():\n        return 1\n    return inner()",
            "lambda x: x * 2",
            "print(f'{3.14159:.2f}')",
            "print('%s items' % 5)",
            "print('{}'.format(5))",
            # input() raises EOFError rather than blocking, because grading
            # runs with stdin=DEVNULL (runner._run_command). Kept allowed for
            # that reason, not by assumption -- an earlier version of this file
            # claimed it hung, which was wrong.
            "name = input('name: ')",
            "print(dir([1, 2]))",
            "print(hasattr(1, 'real'))",
            "print(id(1) > 0)",
            "print(type(1), isinstance(1, int))",
            "class Point:\n"
            "    def __init__(self, x):\n        self.x = x\n"
            "    def __repr__(self):\n        return f'Point({self.x})'\n"
            "    def __eq__(self, other):\n        return self.x == other.x",
            "class Base:\n    pass\n\n\nclass Child(Base):\n    pass",
            "import math\nprint(math.pi)",
            "from math import sqrt\nprint(sqrt(4))",
            "try:\n    x = int('a')\nexcept ValueError:\n    x = 0",
            "while True:\n    break",
            "assert 1 == 1",
            "def fact(n):\n    return 1 if n <= 1 else n * fact(n - 1)",
            "s = 'abc'.upper()\nprint(s[0], s[-1], s[1:2])",
            "print(sorted([3, 1, 2]))",
            "import math\nprint(round(math.sqrt(2), 4))",
            "def gen():\n    yield 1\n\n\nprint(list(gen()))",
        ],
    )
    def test_allows(self, source):
        check_student_code(source)  # must not raise

    def test_allows_unlimited_recursion_helper_lesson(self):
        """The recursion lesson's own shape."""
        source = (
            "def flatten(items):\n"
            "    result = []\n"
            "    for item in items:\n"
            "        if isinstance(item, list):\n"
            "            result.extend(flatten(item))\n"
            "        else:\n"
            "            result.append(item)\n"
            "    return result\n"
        )
        check_student_code(source)


# ── Layer 4: environment scrub ───────────────────────────────────────


class TestEnvironmentScrub:
    def test_does_not_inherit_secrets(self, tmp_path):
        env = build_sandbox_env(tmp_path)
        for leaky in ("GEMINI_API_KEY", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN"):
            assert leaky not in env

    def test_explicitly_withholds_from_a_dirty_parent(self, tmp_path):
        """The real assertion: given secrets in the parent, the child gets none."""
        dirty = {
            "PATH": "/usr/bin",
            "GEMINI_API_KEY": "sk-leak-me",
            "OPENAI_API_KEY": "sk-leak-me-too",
            "AWS_SECRET_ACCESS_KEY": "leak",
            "DUCKY_HOME": "/Users/someone/.ducky",
            "SYSTEMROOT": "C:\\Windows",
        }
        env = build_sandbox_env(tmp_path, base=dirty)
        assert "GEMINI_API_KEY" not in env
        assert "OPENAI_API_KEY" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "DUCKY_HOME" not in env
        assert env["SYSTEMROOT"] == "C:\\Windows"  # platform essential survives

    def test_home_redirected_into_sandbox(self, tmp_path):
        """This is what puts ~/.ducky/config.json out of reach."""
        env = build_sandbox_env(tmp_path)
        assert env["HOME"] == str(tmp_path / "home")
        assert env["TMPDIR"] == str(tmp_path / "tmp")
        assert env["HOME"] != str(Path.home())

    def test_scratch_dir_keeps_work_dir_clean(self, tmp_path):
        """Grading in place must not litter the lesson folder."""
        work = tmp_path / "lesson"
        work.mkdir()
        scratch = tmp_path / "scratch"
        env = build_sandbox_env(work, scratch_dir=scratch)
        assert env["HOME"].startswith(str(scratch))
        assert list(work.iterdir()) == []

    def test_deterministic_grading_env(self, tmp_path):
        env = build_sandbox_env(tmp_path)
        assert env["PYTHONHASHSEED"] == "0"  # reproducible set/dict ordering
        assert env["PYTHONIOENCODING"] == "utf-8"
        assert env["PYTHONUTF8"] == "1"
        assert env["LC_ALL"] == "C.UTF-8"
        assert env["PYTHONNOUSERSITE"] == "1"

    def test_extra_env_is_the_only_way_to_widen(self, tmp_path):
        env = build_sandbox_env(tmp_path, extra={"DUCKY_TEACHER": "1"})
        assert env["DUCKY_TEACHER"] == "1"

    def test_scrubbed_keys_reports_what_was_withheld(self, tmp_path):
        withheld = set(sandbox.scrubbed_env_keys({"PATH": "/x", "SECRET": "y"}))
        assert "SECRET" in withheld
        assert "PATH" not in withheld


# ── Layer 2 + 3: real subprocess containment ─────────────────────────
#
# These are the tests that actually justify calling the thing a sandbox. Each
# spawns a real process and checks a real effect.


def _sandboxed(
    tmp_path: Path,
    code: str,
    *,
    policy: SandboxPolicy | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    """Run ``code`` as python through the full bootstrap + scrubbed env."""
    policy = policy or DEFAULT_POLICY
    boot = write_bootstrap(tmp_path, policy)
    env = build_sandbox_env(tmp_path)
    return subprocess.run(
        [sys.executable, str(boot), sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


@pytest.mark.skipif(os.name != "posix", reason="rlimits are POSIX-only")
class TestResourceLimits:
    def test_interpreter_still_starts(self, tmp_path):
        """A limit that prevents Python from booting is worse than no limit."""
        result = _sandboxed(tmp_path, "print('alive')")
        assert result.returncode == 0
        assert "alive" in result.stdout

    def test_fork_is_refused(self, tmp_path):
        """RLIMIT_NPROC=0 stops helper processes outliving the timeout."""
        result = _sandboxed(tmp_path, "import os\nos.fork()")
        assert result.returncode != 0
        assert "fork" in result.stderr.lower() or "Resource" in result.stderr

    def test_cpu_limit_applies(self, tmp_path):
        result = _sandboxed(
            tmp_path,
            "import resource\nprint('cpu', resource.getrlimit(resource.RLIMIT_CPU)[0])",
        )
        assert result.returncode == 0
        assert result.stdout.strip() != f"cpu {resource.RLIM_INFINITY}"

    def test_open_files_limited(self, tmp_path):
        code = "import resource\nprint('nofile', resource.getrlimit(resource.RLIMIT_NOFILE)[0])"
        result = _sandboxed(tmp_path, code)
        assert result.returncode == 0
        assert result.stdout.strip().endswith("64")

    def test_cannot_read_the_real_ducky_home(self, tmp_path):
        """The regression test for the API-key leak.

        Student code used to inherit the whole parent environment, so
        ``GEMINI_API_KEY`` and ``~/.ducky/config.json`` were both readable from
        inside a graded run.
        """
        home = Path.home()
        ducky_dir = home / ".ducky"
        created = not ducky_dir.exists()
        try:
            ducky_dir.mkdir(exist_ok=True)
            (ducky_dir / "config.json").write_text('{"gemini_api_key":"REAL_SECRET"}')
            result = _sandboxed(
                tmp_path,
                "import os\n"
                "p = os.path.expanduser('~/.ducky/config.json')\n"
                "print('reached_real_home', os.path.exists(p))\n"
                "print('sees_keys', [k for k in os.environ if 'API_KEY' in k])\n",
            )
            assert result.returncode == 0
            assert "reached_real_home False" in result.stdout
            assert "sees_keys []" in result.stdout
        finally:
            (ducky_dir / "config.json").unlink(missing_ok=True)
            if created:
                ducky_dir.rmdir()


# ── Contract: honest self-reporting ──────────────────────────────────


class TestHonestReporting:
    def test_bootstrap_reports_achieved_limits(self, tmp_path):
        """The sidecar must reflect what the kernel did, not what we asked."""
        _sandboxed(tmp_path, "pass")
        applied = read_applied_limits(tmp_path)
        assert applied, "bootstrap wrote no sidecar"
        assert applied.get("cpu_seconds") is True
        assert applied.get("max_processes") is True

    def test_refused_limit_is_reported_as_refused(self, tmp_path):
        """A limit the kernel refuses must be reported as refused.

        macOS caps ``RLIMIT_AS`` below 2 GB, so forcing it on is refused. Linux
        has no such ceiling and grants the 256 MB limit happily. Both outcomes are
        correct -- the point of the test is that the *report* tracks reality, so it
        asserts whichever answer the platform gives and then checks that the
        summary follows it.
        """
        forced = replace(DEFAULT_POLICY, limit_address_space=True)
        _sandboxed(tmp_path, "pass", policy=forced)
        applied = read_applied_limits(tmp_path)
        assert "memory_bytes" in applied, "bootstrap did not report on address space"

        granted = applied["memory_bytes"]
        summary = resolve_limits(forced, applied=applied).enforcement_summary()
        if granted:
            assert "+memory-limit" in summary
            assert "NO-memory-limit" not in summary
        else:
            assert "NO-memory-limit" in summary
            assert "+memory-limit" not in summary.replace("NO-memory-limit", "")

    @pytest.mark.skipif(sys.platform == "darwin", reason="asserts the Linux ceiling behaviour")
    def test_linux_grants_the_address_space_limit(self, tmp_path):
        """Documents the platform split the refusal test above has to tolerate."""
        forced = replace(DEFAULT_POLICY, limit_address_space=True)
        _sandboxed(tmp_path, "print('alive')", policy=forced)
        applied = read_applied_limits(tmp_path)
        assert applied.get("memory_bytes") is True
        # And the interpreter must still have started under it.
        assert resolve_limits(forced, applied=applied).address_space_enforced is True

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS-specific ceiling")
    def test_macos_refuses_the_address_space_limit(self, tmp_path):
        forced = replace(DEFAULT_POLICY, limit_address_space=True)
        _sandboxed(tmp_path, "print('alive')", policy=forced)
        assert read_applied_limits(tmp_path).get("memory_bytes") is False

    def test_darwin_does_not_claim_address_space_by_default(self):
        if sys.platform != "darwin":
            pytest.skip("posix ceiling behaviour is macOS-specific")
        assert DEFAULT_POLICY.resolved_limit_address_space() is False
        assert "NO-memory-limit" in resolve_limits().enforcement_summary()

    def test_network_isolation_is_never_claimed(self, tmp_path):
        """We have no portable way to cut a child off from the network."""
        _sandboxed(tmp_path, "pass")
        limits = resolve_limits(applied=read_applied_limits(tmp_path))
        assert limits.network_isolated is False
        assert "network-allowed" in limits.enforcement_summary()

    def test_weakest_layer_is_named(self, tmp_path):
        _sandboxed(tmp_path, "pass")
        limits = resolve_limits(applied=read_applied_limits(tmp_path))
        assert limits.weakest_layer()

    def test_stale_sidecar_is_cleared_between_runs(self, tmp_path):
        """A crashed run must not make the next one look protected."""
        (tmp_path / sandbox.APPLIED_LIMITS_FILENAME).write_text(
            '{"cpu_seconds": true, "memory_bytes": true}', encoding="utf-8"
        )
        write_bootstrap(tmp_path, DEFAULT_POLICY)
        assert read_applied_limits(tmp_path) == {}

    def test_missing_sidecar_reads_as_nothing_known(self, tmp_path):
        assert read_applied_limits(tmp_path) == {}

    def test_bootstrap_is_not_executable(self, tmp_path):
        path = write_bootstrap(tmp_path)
        assert not os.access(path, os.X_OK)


# ── Output capping ───────────────────────────────────────────────────


class TestTruncateOutput:
    def test_short_output_untouched(self):
        text, truncated = truncate_output("hello", 100)
        assert text == "hello"
        assert truncated is False

    def test_long_output_clamped_with_visible_marker(self):
        text, truncated = truncate_output("x" * 500, 100)
        assert truncated is True
        assert len(text) <= 100 + len("[output truncated")
        assert "truncated" in text

    def test_exact_boundary_is_not_truncated(self):
        text, truncated = truncate_output("x" * 100, 100)
        assert truncated is False
        assert text == "x" * 100

    def test_zero_limit_means_no_cap(self):
        text, truncated = truncate_output("hello", 0)
        assert text == "hello"
        assert truncated is False


# ── Policy derivation ────────────────────────────────────────────────


class TestPolicy:
    def test_timeout_derives_a_shorter_cpu_budget(self):
        from ducky.runner import policy_for_timeout

        policy = policy_for_timeout(8)
        assert policy.wall_seconds == 8
        assert policy.cpu_seconds < policy.wall_seconds

    def test_derive_from_one_second_still_yields_positive_cpu(self):
        from ducky.runner import policy_for_timeout

        assert policy_for_timeout(1).cpu_seconds >= 1

    def test_derive_from_nonsense_still_yields_a_positive_wallclock(self):
        from ducky.runner import policy_for_timeout

        assert policy_for_timeout(0).wall_seconds >= 1
        assert policy_for_timeout(-5).wall_seconds >= 1

    def test_repl_policy_is_tighter_than_the_default_wallclock_is_not(self):
        """The REPL must not feel broken, so it gets a shorter leash."""
        assert sandbox.REPL_POLICY.wall_seconds < DEFAULT_POLICY.wall_seconds

    def test_describe_policy_mentions_the_gap_honestly(self):
        text = sandbox.describe_policy()
        assert "sandbox policy" in text
        if sys.platform == "darwin":
            assert "not enforced" in text

    def test_policy_to_dict_is_serialisable(self):
        import json

        json.dumps(DEFAULT_POLICY.to_dict())


# ── Output flood: the case that motivated incremental draining ──────


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
class TestOutputFlood:
    def test_flooding_child_is_stopped(self, tmp_path):
        """A student printing forever must not exhaust the grader's memory.

        The old implementation used ``communicate()``, which buffers the whole
        stream in the parent before returning anything, so ``while True: print()``
        was bounded only by the wallclock -- 15 seconds of unbounded growth per
        submission. This exercises ``runner._run_command`` itself rather than a
        raw ``subprocess.run``, because the incremental drain and the kill-on-
        overflow live there.
        """
        from ducky.runner import _run_command

        policy = replace(DEFAULT_POLICY, max_output_bytes=64 * 1024, wall_seconds=30)
        boot = write_bootstrap(tmp_path, policy)
        env = build_sandbox_env(tmp_path)
        started = time.monotonic()
        stdout, stderr, returncode = _run_command(
            [sys.executable, str(boot), sys.executable, "-c",
             "while True:\n    print('x' * 4096)\n"],
            cwd=tmp_path,
            env=env,
            timeout=policy.wall_seconds,
            max_output_bytes=policy.max_output_bytes,
        )
        elapsed = time.monotonic() - started
        # The cap should trip long before the 30s wallclock, not after it.
        assert elapsed < 20, f"overflow kill did not trip promptly ({elapsed:.1f}s)"
        assert len(stdout) <= policy.max_output_bytes + 200
        assert "DuckyOutputLimitError" in stdout or "DuckyOutputLimitError" in stderr
        assert returncode == 125

    def test_normal_output_is_not_truncated(self, tmp_path):
        from ducky.runner import _run_command

        policy = replace(DEFAULT_POLICY, max_output_bytes=64 * 1024)
        boot = write_bootstrap(tmp_path, policy)
        env = build_sandbox_env(tmp_path)
        stdout, _stderr, returncode = _run_command(
            [sys.executable, str(boot), sys.executable, "-c", "print('small output')"],
            cwd=tmp_path,
            env=env,
            timeout=30,
            max_output_bytes=policy.max_output_bytes,
        )
        assert returncode == 0
        assert "small output" in stdout
        assert "truncated" not in stdout

    def test_spinning_child_hits_the_cpu_limit_before_the_wallclock(self, tmp_path):
        """A tight infinite loop must be stopped by RLIMIT_CPU, not the timeout.

        CPU budget is deliberately set below the wallclock so the diagnosis can
        be specific. A generic "timed out" for a student who wrote
        ``while True: pass`` is much less useful than "you ran out of CPU",
        because it points at the loop instead of at the clock.

        The signal the kill arrives as is platform-dependent -- SIGXCPU on macOS,
        SIGKILL on Linux when soft and hard limits coincide -- so this asserts the
        report we produce rather than the mechanism the kernel chose.
        """
        from ducky.runner import _run_command

        policy = replace(DEFAULT_POLICY, wall_seconds=30, cpu_seconds=1)
        boot = write_bootstrap(tmp_path, policy)
        env = build_sandbox_env(tmp_path)
        started = time.monotonic()
        _stdout, stderr, returncode = _run_command(
            [sys.executable, str(boot), sys.executable, "-c", "while True:\n    pass\n"],
            cwd=tmp_path,
            env=env,
            timeout=policy.wall_seconds,
            max_output_bytes=policy.max_output_bytes,
        )
        elapsed = time.monotonic() - started
        assert elapsed < 20, f"spinning child was not stopped promptly ({elapsed:.1f}s)"
        assert "DuckyCpuLimitError" in stderr
        assert "never finishes" in stderr
        # Reported as a timeout-equivalent so diagnostics classify it as one.
        assert returncode == 124

    def test_cpu_limit_kill_classifies_as_timeout(self, tmp_path):
        """Regression: the CPU kill must not degrade into a runtime error.

        ``diagnostics`` keys off marker text, so an unrecognised marker sent an
        infinite loop to ``runtime_error`` -- strictly worse for a student than
        the ``timeout`` they used to get.
        """
        from ducky.diagnostics import classify_failure

        diagnosis = classify_failure(
            expected=None,
            actual=None,
            stdout="",
            error="DuckyCpuLimitError: run exceeded its CPU budget and was stopped.",
            raw_output="",
        )
        assert diagnosis == "timeout"

    def test_silent_sleeping_child_is_stopped_by_the_wallclock(self, tmp_path):
        """A child that sleeps rather than spins must still be bounded.

        ``time.sleep`` consumes no CPU, so RLIMIT_CPU never fires and the
        wallclock is the only thing that can stop it. Both paths need covering --
        otherwise a policy change that breaks one of them is invisible.
        """
        from ducky.runner import _run_command

        policy = replace(DEFAULT_POLICY, wall_seconds=2, cpu_seconds=30)
        boot = write_bootstrap(tmp_path, policy)
        env = build_sandbox_env(tmp_path)
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            _run_command(
                [sys.executable, str(boot), sys.executable, "-c",
                 "import time\ntime.sleep(60)\n"],
                cwd=tmp_path,
                env=env,
                timeout=policy.wall_seconds,
                max_output_bytes=policy.max_output_bytes,
            )
        assert time.monotonic() - started < 20

    def test_stderr_is_capped_too(self, tmp_path):
        from ducky.runner import _run_command

        policy = replace(DEFAULT_POLICY, max_output_bytes=32 * 1024, wall_seconds=30)
        boot = write_bootstrap(tmp_path, policy)
        env = build_sandbox_env(tmp_path)
        _stdout, stderr, _rc = _run_command(
            [sys.executable, str(boot), sys.executable, "-c",
             "import sys\nwhile True:\n    sys.stderr.write('e' * 4096)\n"],
            cwd=tmp_path,
            env=env,
            timeout=policy.wall_seconds,
            max_output_bytes=policy.max_output_bytes,
        )
        assert len(stderr) <= policy.max_output_bytes + 200
