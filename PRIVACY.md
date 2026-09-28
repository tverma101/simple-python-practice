# Privacy Policy

Ducky is local-first.

- Ducky does not upload reports automatically.
- Diagnostic reports stay on the user's computer unless manually exported.
- Dataset exports are created manually.
- Users should review exports before sharing them.
- No cloud sync by default.
- No automatic telemetry.
- No public student data.
- Teacher backend submission and fetch steps are opt-in and only run when the user configures a backend URL or passes the matching `ducky student` flags.
- Raw upload mode is also opt-in; when enabled, it sends the unredacted code, stdout, stderr, and failure details to the configured teacher backend.

## Data Storage

All data is stored locally in `~/.ducky/`:

- `reports/` - anonymized diagnostic reports
- `dataset/` - exported JSONL files
- `logs/` - event logs
- `progress.db` - local progress tracking

## Redaction

Before export, Ducky automatically redacts:

- Email addresses
- Phone numbers
- Absolute file paths
- API keys and tokens
- Personal comments

When a teacher backend is enabled, Ducky still redacts report content before submission unless the calling workflow is explicitly changed to send something else.

## Manual Export

Dataset JSONL exports must be initiated manually by the user. No data is ever sent anywhere without explicit user action.
