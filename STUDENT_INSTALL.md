# Ducky — Student Install Guide

Ducky is a local-first Python coding tutor. It works offline with smart rule-based hints — no LLM or API key needed.

## Quick Install (one command)

Paste this in your terminal:

```bash
curl -sSL https://pub-be8cbd81214d42469aa45b91d710100b.r2.dev/student-install.sh | bash
```

The installer resolves the latest student archive from the public Cloudflare
R2 manifest. It does not install the teacher/source build.

Then run:

```bash
ducky
```

That's it. Ducky opens the interactive menu where you can start lessons, run tests, and get hints.

---

## Auto-Update

Ducky Student checks the Cloudflare R2 `latest.json` manifest on every launch
and auto-updates in the background. No need to reinstall.

To enable auto-update (on by default):

```bash
ducky config set --auto-update-app true
```

---

## Optional: Gemini AI Hints (free)

Ducky's built-in rule-based hints work great without any LLM. If you want smarter AI-powered hints:

1. Get a free API key at https://aistudio.google.com/apikey
2. Run:
   ```bash
   ducky setup-llm
   ```
3. Choose option 1 (Gemini) and paste your key

---

## Commands

```bash
ducky                          # Open interactive menu
ducky test                     # Run tests on current lesson
ducky test --lesson <name>     # Run tests on a specific lesson
ducky hint                     # Show hint for last failure
ducky instructions             # View lesson instructions
ducky open                     # Open lesson in editor
ducky init [dir]               # Initialize a Ducky workspace
ducky student update           # Pull lessons your teacher shared (or drop a .duckypack into assignments/)
ducky setup-llm                # Configure LLM hints (optional)
ducky student connect ...      # Connect to teacher server (classroom mode)
ducky student update           # Pull new lessons from teacher
```

---

## Requirements

- Python 3.10+
- Works on macOS, Linux, and Windows (WSL)
- No internet needed (except for auto-update and optional Gemini hints)
