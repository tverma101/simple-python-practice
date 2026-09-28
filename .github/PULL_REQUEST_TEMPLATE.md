<!--
Short. If a box cannot be ticked, say why in a comment rather than deleting the
item — "the browser check needs Chrome and this box has none" is information.
-->

## What this changes

<!-- One or two sentences. -->

## Checklist

- [ ] Tests added or updated for this change.
- [ ] **The new test was shown to FAIL against the code it guards before it was
      shown to pass.** Paste both outputs in a comment. A test that has never
      failed is not evidence — see CONTRIBUTING.md. This matters most for
      anything touching the sandbox, the runner, the content store, or a route.
- [ ] `python -m pytest` passes.
- [ ] `.venv/bin/ruff check src tests tools` is clean.
- [ ] `python tools/check_editor.py` run, **or** a comment saying why it could
      not be (it needs Chrome or Chromium).
- [ ] No runtime dependencies added. If one was unavoidable, that is a
      conversation to have in an issue first, not in a diff.
- [ ] If this touches the security model: `docs/SECURITY_MODEL.md` updated in
      the same change, and no existing claim weakened to make a test pass. A
      protection that is absent gets documented as absent.
- [ ] If this touches the problem library: it goes through
      `tools/build_content.py` verification. No hand-inserted rows.
- [ ] One concern per pull request.

## Security

If this is or touches a vulnerability, do not open a pull request — report it
privately first, as described in `SECURITY.md`.
