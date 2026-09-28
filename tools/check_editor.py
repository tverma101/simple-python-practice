#!/usr/bin/env python3
"""Verify the vendored CodeMirror build actually loads in a real browser.

Exists because of a specific, silent failure mode. CodeMirror 6 ships as ES
modules that import each other by bare specifier, resolved through an import map.
If any specifier is missing from that map the browser fails the fetch and
**CodeMirror does not report it** -- no console error, no exception, no
try/catch. The result is a silently dead editor that looks fine in a screenshot.

The previous codebase had the same class of failure in a worse form: its only
script tag was a CDN URL, and when that failed the page rendered as a static image
with every button inert and no error shown.

So this asserts on *rendered DOM*:

  - the editor mounts and its content surface carries role=textbox
  - a Python keyword is wrapped in a token span, which proves the Lezer grammar
    and the highlight style both loaded -- a mounted-but-unparsed editor would
    not do that
  - line numbers render
  - the dark theme applies
  - no unhandled errors, no failed fetches

The page reports back by POSTing to this server, so no DevTools client is
needed. Console errors are captured in-page by wrapping console.error before any
module loads, which is the only place early enough to see them.

Usage:
    python tools/check_editor.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>editor check</title>
<style>
  html,body { margin:0; background:#0d1117; color:#e6edf3; font:14px system-ui; }
  #host { height: 320px; }
  .cm-editor { height: 100%; }
</style>
<script>
// Installed before any module runs, so it catches import-time failures --
// which is the whole point, since CodeMirror swallows them.
window.__ERRORS__ = [];
const realError = console.error.bind(console);
console.error = (...a) => { window.__ERRORS__.push(a.map(String).join(' ')); realError(...a); };
window.addEventListener('error', (e) => window.__ERRORS__.push('error: ' + (e.message || e)));
window.addEventListener('unhandledrejection', (e) => window.__ERRORS__.push('rejection: ' + e.reason));
</script>
<script type="importmap">__IMPORTMAP__</script>
</head>
<body>
<div id="host"></div>
<script type="module">
const report = async (payload) => {
  payload.errors = window.__ERRORS__ || [];
  try { await fetch('/result', { method: 'POST', body: JSON.stringify(payload) }); }
  catch (e) { /* the harness is gone; nothing to do */ }
};
try {
  const { EditorView, keymap, lineNumbers, highlightActiveLine, drawSelection,
          rectangularSelection, crosshairCursor, highlightSpecialChars } =
    await import('@codemirror/view');
  const { EditorState, Compartment } = await import('@codemirror/state');
  const { history, historyKeymap, defaultKeymap, indentWithTab } =
    await import('@codemirror/commands');
  const { searchKeymap, highlightSelectionMatches } = await import('@codemirror/search');
  const { defaultHighlightStyle, syntaxHighlighting, bracketMatching,
          indentOnInput, foldGutter, foldKeymap } =
    await import('@codemirror/language');
  // closeBrackets lives in @codemirror/autocomplete, not @codemirror/language.
  const { autocompletion, closeBrackets, closeBracketsKeymap } =
    await import('@codemirror/autocomplete');
  const { python } = await import('@codemirror/lang-python');
  const { oneDark } = await import('@codemirror/theme-one-dark');

  const theme = new Compartment();
  const doc = 'def greet(name):\\n    return "hi " + name\\n';
  const view = new EditorView({
    parent: document.getElementById('host'),
    state: EditorState.create({
      doc,
      extensions: [
        lineNumbers(), highlightActiveLine(), drawSelection(),
        rectangularSelection(), crosshairCursor(), highlightSpecialChars(),
        history(), foldGutter(), indentOnInput(), bracketMatching(),
        closeBrackets(), autocompletion(), highlightSelectionMatches(),
        syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
        python(),
        keymap.of([...closeBracketsKeymap, ...defaultKeymap, ...historyKeymap,
                   ...searchKeymap, ...foldKeymap, indentWithTab]),
        EditorView.theme({ '&': { height: '100%' },
                           '.cm-scroller': { fontFamily: 'Menlo, monospace' } }),
        EditorView.contentAttributes.of({ 'aria-label': 'Python code editor' }),
        // Light by default; the dark theme is swapped in below via this
        // compartment, which is also what proves compartments work.
        theme.of([]),
      ],
    }),
  });

  const surface = document.querySelector('.cm-content');
  const spans = Array.from(document.querySelectorAll('.cm-content span'));
  const light = {
    mounted: !!surface,
    role: surface && surface.getAttribute('role'),
    ariaMultiline: surface && surface.getAttribute('aria-multiline'),
    ariaLabel: surface && surface.getAttribute('aria-label'),
    contentEditable: surface && surface.getAttribute('contenteditable'),
    tokenSpans: spans.length,
    lineNumbers: document.querySelectorAll('.cm-lineNumbers').length,
    lineNumberEls: document.querySelectorAll('.cm-gutterElement').length,
    docText: surface && surface.textContent,
    // A mounted-but-unparsed editor has zero token spans; this is the check
    // that distinguishes "loaded" from "actually working".
    tokenTexts: spans.slice(0, 6).map((s) => s.textContent),
  };

  // Swapping a compartment proves the extension set is reconfigurable and that
  // the dark theme is a real stylesheet injection rather than a class name.
  const before = getComputedStyle(document.querySelector('.cm-editor')).backgroundColor;
  view.dispatch({ effects: theme.reconfigure(oneDark) });
  await new Promise((r) => setTimeout(r, 120));
  const after = getComputedStyle(document.querySelector('.cm-editor')).backgroundColor;

  // Typing must actually change the document, or the editor is inert.
  view.dispatch({ changes: { from: doc.length, insert: '\\n' } });
  const afterTyping = view.state.doc.toString();

  // Debug aid: every class actually present, so a selector mismatch is
  // visible rather than showing up as a mysterious zero.
  const classes = new Set();
  document.querySelectorAll('.cm-editor *').forEach((el) => {
    (el.classList || []).forEach && Array.from(el.classList).forEach((c) => classes.add(c));
  });

  await report({
    ok: true,
    allClasses: Array.from(classes),
    light,
    darkColorBefore: before,
    darkColorAfter: after,
    darkChanged: before !== after,
    typingWorked: afterTyping.length === doc.length + 1,
    docLength: doc.length,
  });
} catch (err) {
  await report({ ok: false, error: String((err && err.stack) || err) });
}
</script>
</body>
</html>
"""


# ── the module, not just CodeMirror ─────────────────────────────────
#
# The page above constructs its own EditorView, so it exercises the vendored
# CodeMirror build but never static/js/editor.js -- the file where the real bug
# lived. `onChange` called `view.updateListener.of(...)`, but updateListener is a
# static Facet on the *constructor*, so `view.updateListener` was undefined and
# the TypeError escaped initEditor's try/catch. Nothing above would have noticed:
# the editor still mounted and still looked perfect, and only autosave was dead.
#
# This drives the shipped module exactly as the problem page does.
AUTOSAVE_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<style>html,body{margin:0;background:#fff;font:14px system-ui}
#editor-host{height:220px}.cm-editor{height:100%}</style>
<script>
window.__ERRORS__ = [];
const realError = console.error.bind(console);
console.error = (...a) => { window.__ERRORS__.push(a.map(String).join(' ')); realError(...a); };
window.addEventListener('error', (e) => window.__ERRORS__.push('error: ' + (e.message || e)));
window.addEventListener('unhandledrejection', (e) => window.__ERRORS__.push('rejection: ' + e.reason));
</script>
<script type="importmap">__IMPORTMAP__</script>
</head>
<body>
<div id="editor-host"></div>
<textarea id="editor-fallback"></textarea>
<script type="module">
const report = async (payload) => {
  payload.errors = window.__ERRORS__ || [];
  try { await fetch('/result', { method: 'POST', body: JSON.stringify(payload) }); }
  catch (e) { /* the harness is gone; nothing to do */ }
};
// Watchdog: without it a hang is indistinguishable from a module failure, which
// is exactly the confusion this check exists to prevent.
const watchdog = setTimeout(() => report({
  ok: false, error: 'watchdog: the page never finished (8s)',
}), 8000);
try {
  // The shipped module, imported exactly as problem.js imports it.
  const editor = await import('/static/js/editor.js');
  await editor.initEditor('def add_two(a, b):\n    return 0\n');

  let calls = 0;
  let fired = null;
  // Registered before any edit, as problem.js does.
  editor.onChange((text) => { calls += 1; fired = text; });

  const mounted = !editor.isFallback();
  const before = editor.getCode();

  // EditorView.findFromDOM is the supported way to get at the live view from
  // outside, so a real document change can be dispatched. This is the only way
  // to prove onChange fires: the previous bug threw in onChange itself, so the
  // handler was never installed and nothing about mounting would reveal it.
  const { EditorView } = await import('@codemirror/view');
  const view = EditorView.findFromDOM(document.querySelector('.cm-editor'));
  if (!view) throw new Error('findFromDOM returned null: no live editor to drive');
  view.dispatch({ changes: { from: view.state.doc.length, insert: '\n# edited' } });
  await new Promise((r) => setTimeout(r, 250));

  // A second change, to prove it is not a one-shot.
  view.dispatch({ changes: { from: 0, insert: '# note\n' } });
  await new Promise((r) => setTimeout(r, 250));

  const after = editor.getCode();
  clearTimeout(watchdog);
  await report({
    ok: true,
    mounted,
    handlerCalls: calls,
    handlerFired: calls > 0,
    lastTextMatchesDoc: fired === after,
    lastFired: fired,
    after,
    codeChanged: after !== before,
    firstLineAfter: after.split('\n')[0],
  });
} catch (err) {
  clearTimeout(watchdog);
  await report({ ok: false, error: String((err && err.stack) || err) });
}
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    result: dict | None = None
    root: Path = Path(".")
    page: str = ""
    autosave: str = ""

    def log_message(self, *args):  # noqa: A003 - silence the request log
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - http.server API
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send(200, Handler.page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/autosave.html":
            self._send(200, Handler.autosave.encode("utf-8"), "text/html; charset=utf-8")
            return
        # Serve the vendored editor files. An import map that 404s fails with
        # "Failed to resolve module specifier" and nothing else, so this has to
        # actually work for the check to mean anything.
        target = (Handler.root / path.lstrip("/")).resolve()
        try:
            target.relative_to(Handler.root.resolve())
        except ValueError:
            self._send(403, b"forbidden", "text/plain")
            return
        if target.is_file():
            ctype = {
                ".js": "text/javascript; charset=utf-8",
                ".mjs": "text/javascript; charset=utf-8",
                ".json": "application/json; charset=utf-8",
                ".css": "text/css; charset=utf-8",
            }.get(target.suffix, "application/octet-stream")
            self._send(200, target.read_bytes(), ctype)
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            Handler.result = json.loads(raw.decode("utf-8"))
        except ValueError:
            Handler.result = {"ok": False, "error": "unparseable result body"}
        self._send(204, b"", "text/plain")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_chrome() -> str | None:
    import shutil as _shutil

    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    for name in ("google-chrome", "chromium", "chrome"):
        found = _shutil.which(name)
        if found:
            return found
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=45.0)
    args = parser.parse_args()

    chrome = find_chrome()
    if not chrome:
        print("SKIP: no Chrome or Chromium found; cannot verify the editor")
        return 0

    workdir = Path(tempfile.mkdtemp(prefix="cm-check-"))
    # Serve the real `static/` tree at the same path the app serves it at, so
    # this check exercises production URLs rather than a convenient mirror.
    # Copy rather than symlink: the handler guards against path traversal by
    # resolving paths, and a symlink out of the served root would (correctly) be
    # rejected.
    #
    # This exact mismatch already bit once -- the import map uses absolute
    # /static/... specifiers (they must, because the document lives at
    # /p/{slug} and a relative specifier resolves to /p/vendor/... and fails
    # silently), and serving the vendor tree at the root made every specifier a
    # 404 while the test still passed a naive URL check.
    shutil.copytree(REPO / "static", workdir / "static",
                    ignore=shutil.ignore_patterns("*.LICENSE"))

    importmap = (REPO / "static" / "vendor" / "cm" / "importmap.json").read_text(
        encoding="utf-8"
    )
    Handler.page = PAGE_TEMPLATE.replace("__IMPORTMAP__", importmap.strip())
    Handler.autosave = AUTOSAVE_PAGE.replace("__IMPORTMAP__", importmap.strip())
    Handler.result = None
    Handler.root = workdir
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    profile = tempfile.mkdtemp(prefix="cm-chrome-")
    proc = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            f"--user-data-dir={profile}",
            "--virtual-time-budget=20000",
            f"http://127.0.0.1:{port}/index.html",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline and Handler.result is None:
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        first = Handler.result

        # Second page, same browser: the shipped editor module and its change
        # handler. Runs in a fresh tab so the first page's state cannot mask a
        # failure here.
        Handler.result = None
        if first is not None:
            # Shut the first browser down first. Two headless Chromes sharing a
            # launch can leave the second one never starting, and then the module
            # page looks like a module failure rather than a harness failure.
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            proc2 = subprocess.Popen(
                [chrome, "--headless=new", "--disable-gpu", "--no-first-run",
                 "--no-default-browser-check", "--disable-extensions",
                 f"--user-data-dir={profile}-2", "--virtual-time-budget=20000",
                 f"http://127.0.0.1:{port}/autosave.html"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + args.timeout
                while time.monotonic() < deadline and Handler.result is None:
                    if proc2.poll() is not None:
                        break
                    time.sleep(0.2)
            finally:
                proc2.terminate()
                try:
                    proc2.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc2.kill()
        module_result = Handler.result or {"ok": False, "error": "no result from autosave page"}
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            proc.kill()
        server.shutdown()
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(profile, ignore_errors=True)

    result = first if "first" in dir() else Handler.result
    if result is None:
        print("FAIL: the page never reported a result")
        print("      (the editor module graph probably failed to resolve)")
        return 1

    if not result.get("ok"):
        print("FAIL: the page threw during initialisation\n")
        print(result.get("error", "<no error>"))
        for err in result.get("errors", []):
            print(f"  console: {err}")
        return 1

    light = result.get("light", {})
    errors = result.get("errors") or []
    module = module_result if isinstance(module_result, dict) else {}
    module_errors = module.get("errors") or []
    checks = [
        ("editor mounted", light.get("mounted") is True),
        ("role=textbox on the content surface", light.get("role") == "textbox"),
        ("aria-multiline set", light.get("ariaMultiline") == "true"),
        ("accessible name present", light.get("ariaLabel") == "Python code editor"),
        ("contenteditable set", light.get("contentEditable") == "true"),
        ("line numbers rendered", (light.get("lineNumbers") or 0) >= 1),
        # The check that separates "loaded" from "working": a mounted editor with
        # no token spans means the grammar or highlight style never loaded.
        ("python tokens rendered", (light.get("tokenSpans") or 0) > 0),
        ("doc text intact", "def greet" in (light.get("docText") or "")),
        ("dark theme applies", result.get("darkChanged") is True),
        ("typing updates the document", result.get("typingWorked") is True),
        ("no console errors", not errors),
    ]

    # The shipped module, not just the vendored build. This is the check that
    # would have caught the dead autosave: the editor mounted and looked perfect
    # while onChange threw, so every assertion above passed.
    module_checks = [
        ("static/js/editor.js mounts (not fallback)", module.get("mounted") is True),
        ("onChange handler actually fires", module.get("handlerFired") is True),
        ("onChange fires on every change", (module.get("handlerCalls") or 0) >= 2),
        ("handler receives the current document", module.get("lastTextMatchesDoc") is True),
        ("code changed through the editor", module.get("codeChanged") is True),
        ("no module console errors", not module_errors),
    ]

    for label, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    print()
    print("  static/js/editor.js  (autosave wiring)")
    for label, ok in module_checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if module.get("error"):
        print(f"        error: {module['error']}")
    for err in module_errors:
        print(f"        console: {err}")
    print()
    print(f"  token spans: {light.get('tokenSpans')}  "
          f"first tokens: {light.get('tokenTexts')}")
    print(f"  lineNumbers={light.get('lineNumbers')} "
          f"gutterElements={light.get('lineNumberEls')}")
    if os.environ.get("CM_DEBUG"):
        print("  classes:", " ".join(sorted(result.get("allClasses") or [])))
    print(f"  colour: {result.get('darkColorBefore')} -> {result.get('darkColorAfter')}")
    if errors:
        print("\n  console errors:")
        for err in errors[:10]:
            print(f"    {err}")

    failed = [label for label, ok in checks + module_checks if not ok]
    print()
    if failed:
        print(f"{len(failed)} check(s) failed: {', '.join(failed)}")
        return 1
    print("all editor checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
