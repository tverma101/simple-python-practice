---
name: Bug report
about: Something in the practice server, the grader, or the editor does not work
title: ''
labels: bug
assignees: ''
---

<!--
If this is a sandbox escape, a sandbox bypass, or anything that lets a student
reach data the security model says they cannot reach: STOP. Do not file this.

Report it privately instead — see SECURITY.md. A public issue is a published
reproduction with a notification attached.
-->

## What happened

<!-- What you observed. The actual message, the actual output, the actual response. -->

## What you expected

## Steps to reproduce

1.
2.
3.

## Environment

Paste the output of:

```bash
python -VV
```

- Operating system:
- Browser, if the problem is in the page:
- How the server was started (e.g. `ducky web --host 127.0.0.1`, or `ducky web --allow-lan`):

## Content

If a specific problem is involved, which one (the `/p/{slug}` URL or the problem id)?

<!--
If the problem is a specific exercise rather than the platform, say so here —
a broken exercise is a content bug, and it is fixed in the content pipeline, not
in the server. See docs/CONTENT_VERIFICATION.md.
-->

## Anything else

Logs, screenshots, a failing test, or the submission that shows the behaviour.
Redact API keys, access codes, and anything belonging to a real student.
