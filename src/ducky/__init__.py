__version__ = "0.5.2"

# Replaced by the student build script with a fingerprint of the packaged
# source so same-version Cloudflare releases can still be detected.
__build_id__ = "source"

# Build type — "full" (teacher build) or "student" (student-only build).
# The student build script patches this to "student" and appends "s" to __version__.
# Code checks this to show the right banner, control update timing, etc.
__build_type__ = "full"

# Set these before distributing so auto-update knows where to look.
# Students can override via config (github_owner / github_repo).
GITHUB_OWNER = "tverma101"
GITHUB_REPO = "simple-python-practice"

# Tracks the source of the version info currently in use.
# "shipped" — the version baked into the installed package at build time.
# "live-checked" — verified live against GitHub releases.
# "pending-update" — an update was downloaded and will apply on restart.
__version_source__ = "shipped"
