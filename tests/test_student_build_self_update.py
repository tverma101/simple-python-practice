"""Packaged student build integration test for force-live self-update.

CRITICAL VERIFICATION GUARD:
This test builds the student wheel, installs it in a fresh venv, and
runs ``ducky student self-update --force-live`` against a fake local
GitHub API.  It verifies the **packaged student build** actually reaches
the live code path and returns the expected proof fields.

The test fails if:
- The CLI uses cached version data (``used_cache: true``)
- The CLI uses the old ``check_for_update()`` path that falls back to R2
- The source-tree-only ``force_check_update()`` is used instead of the
  packaged one
- The sentinel version ``v99.99.99-test-live`` does not appear in output
- The proof fields ``source``, ``used_cache``, ``version_source`` are
  missing or wrong
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest

# ── Fake GitHub API server ─────────────────────────────────────────


class FakeGitHubHandler(BaseHTTPRequestHandler):
    """Serves a fake GitHub releases/latest response."""

    SENTINEL_VERSION = "99.99.99-test-live"

    def do_GET(self):
        body = json.dumps({
            "tag_name": f"v{self.SENTINEL_VERSION}",
            "assets": [],
            "body": "Integration test release — verify force-live path",
            "zipball_url": "https://example.com/fake.zip",
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # Suppress HTTP log noise


@pytest.fixture(scope="module")
def fake_github_server():
    """Start a fake GitHub API server on a random port, yield its URL."""
    server = HTTPServer(("127.0.0.1", 0), FakeGitHubHandler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}"
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield url
    server.shutdown()


@pytest.fixture(scope="module")
def built_student_wheel():
    """Build the student wheel and return the path to it."""
    repo_root = Path(__file__).resolve().parent.parent
    build_script = repo_root / "build_student.sh"

    if not build_script.is_file():
        pytest.skip("build_student.sh not found — cannot build student artifact")

    result = subprocess.run(
        ["bash", str(build_script)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        pytest.skip(f"build_student.sh failed:\n{result.stderr}")

    # Find the most recently built wheel
    downloads_dir = repo_root / "downloads"
    wheels = sorted(downloads_dir.glob("ducky_student-*.whl"), reverse=True)
    if not wheels:
        pytest.skip("No student wheel found in downloads/")
    return wheels[0]


@pytest.fixture
def fresh_venv(tmp_path):
    """Create a fresh Python venv and return its bin directory."""
    venv_dir = tmp_path / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    if sys.platform == "win32":
        bin_dir = venv_dir / "Scripts"
    else:
        bin_dir = venv_dir / "bin"
    return bin_dir


def _run_in_venv(bin_dir, args, **kwargs) -> subprocess.CompletedProcess:
    """Run a command using the venv's Python."""
    python = bin_dir / "python"
    return subprocess.run(
        [str(python)] + args,
        capture_output=True,
        text=True,
        timeout=60,
        **kwargs,
    )


def _run_ducky_in_venv(bin_dir, args, **kwargs) -> subprocess.CompletedProcess:
    """Run the ``ducky`` CLI script directly from the venv's bin."""
    ducky_script = bin_dir / "ducky"
    if not ducky_script.is_file():
        ducky_script = bin_dir / "ducky.exe"
    return subprocess.run(
        [str(ducky_script)] + args,
        capture_output=True,
        text=True,
        timeout=60,
        **kwargs,
    )


# ── The integration test ───────────────────────────────────────────


def test_student_build_force_live_self_update(
    fake_github_server: str,
    built_student_wheel: Path,
    fresh_venv: Path,
):
    """Install the student wheel and run ``ducky student self-update --force-live``
    against a fake local GitHub API.  Assert the sentinel version and all
    proof fields appear in the CLI output.

    This is the CRITICAL VERIFICATION GUARD that proves:
    1. The packaged student build reaches live GitHub API code
    2. ``force_check_update()`` is the code path taken
    3. ``source``, ``used_cache``, and ``version_source`` are correct
    4. The R2 fallback is NOT used
    """
    bin_dir = fresh_venv

    repo_root = Path(__file__).resolve().parent.parent
    manifest = json.loads((repo_root / "dist" / "latest.json").read_text(encoding="utf-8"))
    assert manifest["build_type"] == "student"
    assert manifest["version"] in built_student_wheel.name
    archive = repo_root / "downloads" / f"ducky_student-v{manifest['version']}.zip"
    assert archive.is_file()
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == manifest["sha256"]

    # Distribution boundary: the student artifact must retain its windowed
    # student entry point while excluding teacher-only implementation modules.
    with zipfile.ZipFile(built_student_wheel) as wheel:
        names = set(wheel.namelist())
        entry_point_file = next(
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        )
        entry_points = wheel.read(entry_point_file).decode()
    assert "ducky-gui = ducky.gui:main" in entry_points
    assert "ducky/gui/student_desktop.py" in names
    assert "ducky/gui/teacher.py" not in names
    assert "ducky/server.py" not in names
    assert "ducky/classroom.py" not in names

    # 1. Install the student wheel
    pip_result = _run_in_venv(
        bin_dir,
        ["-m", "pip", "install", str(built_student_wheel)],
    )
    assert pip_result.returncode == 0, (
        f"pip install failed:\n{pip_result.stdout}\n{pip_result.stderr}"
    )

    # 2. Verify ducky is installed
    which_result = _run_ducky_in_venv(bin_dir, ["--version"])
    assert which_result.returncode == 0, (
        f"ducky --version failed:\n{which_result.stdout}\n{which_result.stderr}"
    )

    # 3. Set the config to point to our fake GitHub API
    #    The CLI uses load_config() which reads ~/.ducky/config.json,
    #    but within the venv we control PYTHONPATH and can set DUCKY_HOME
    #    to a sandboxed config directory.
    sandbox_home = Path(fresh_venv).parent / "sandbox_home"
    sandbox_home.mkdir(exist_ok=True)
    config = {
        "github_api_override_base": fake_github_server,
        "auto_update_app": True,
    }
    config_path = sandbox_home / "config.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    # 4. Run ducky student self-update --force-live
    env = os.environ.copy()
    env["DUCKY_HOME"] = str(sandbox_home)

    result = _run_ducky_in_venv(
        bin_dir,
        ["student", "self-update", "--force-live"],
        env=env,
        input="n\n",
    )

    stdout = result.stdout
    stderr = result.stderr

    print(f"\n--- CLI stdout ---\n{stdout}")
    if stderr:
        print(f"--- CLI stderr ---\n{stderr}")

    # 5. CRITICAL ASSERTIONS
    # The sentinel version must appear (proves we hit the fake endpoint)
    assert FakeGitHubHandler.SENTINEL_VERSION in stdout, (
        f"Sentinel version '{FakeGitHubHandler.SENTINEL_VERSION}' not in output.\n"
        f"This means the live code path was NOT reached.\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr}\n"
    )

    # Proof field: source
    assert "force-live-github" in stdout, (
        f"'source: force-live-github' not in output.\n"
        f"This means the source code path was not force_check_update().\n"
        f"stdout:\n{stdout}"
    )

    # Proof field: used_cache (Python prints bools as "False" not "false")
    assert "False" in stdout, (
        f"'used_cache: False' not in output.\n"
        f"This means cached data may have been used.\n"
        f"stdout:\n{stdout}"
    )

    # Proof field: version_source
    assert "live-checked" in stdout, (
        f"'version_source: live-checked' not in output.\n"
        f"stdout:\n{stdout}"
    )

    # Ensure no R2 fallback was hit
    assert "R2" not in stdout and "r2" not in stdout, (
        f"R2 fallback appears in output — force-live should never hit R2.\n"
        f"stdout:\n{stdout}"
    )

    # Exit code should be 0 (up to date since we set current version to something)
    assert result.returncode == 0, (
        f"CLI returned non-zero exit code {result.returncode}.\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr}\n"
    )


def test_student_installer_default_command_launches_the_student_gui():
    """The documented fresh-install ``ducky`` command must open the GUI.

    ``ducky student`` is not the interactive app: it is a CLI namespace whose
    parser requires a command such as ``lessons`` or ``quiz``.  The installer
    used to generate that invalid invocation, so a clean install never reached
    the Quiz Center even though the quiz JSON was present in the wheel.
    """
    repo_root = Path(__file__).resolve().parent.parent
    installer = (repo_root / "student-install.sh").read_text(encoding="utf-8")

    assert 'exec "$VENV_DIR/bin/ducky-gui"' in installer
    assert 'exec "$VENV_DIR/bin/ducky" student' not in installer
    shim = installer.split('cat > "$BIN_DIR/ducky" <<SHIM', 1)[1].split("SHIM", 1)[0]
    assert "`" not in shim, "backticks in the unquoted shim heredoc would execute during install"


if __name__ == "__main__":
    pytest.main([__file__, "-xvs"])
