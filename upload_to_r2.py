#!/usr/bin/env python3
"""Upload Ducky distribution files to Cloudflare R2.

Single canonical uploader. Pure stdlib (AWS SigV4 over urllib) — no aws CLI,
no extra dependencies, consistent with the repo's "only pytest at runtime" rule.

Config (any one path works):
  A. Run `bash setup_r2.sh` once. It writes ~/.aws/credentials [r2] and
     ~/.aws/config [profile r2] (with s3.endpoint_url). Nothing else needed.
  B. Env vars: R2_ACCOUNT_ID, and optionally R2_ACCESS_KEY_ID /
     R2_SECRET_ACCESS_KEY (otherwise read from ~/.aws/credentials [r2]).
  C. R2_BUCKET to override the default public bucket.

Uploads the student artifacts in downloads/, the generated dist/latest.json,
and student-install.sh from the repo root. Versioned artifacts are preserved,
and stable student-latest.zip / student-latest.whl aliases are refreshed on
every upload so public links never need a version number.

  python3 upload_to_r2.py            # upload latest student release + installer
  python3 upload_to_r2.py --dry-run  # show what would be uploaded
"""
from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

# ── Config ──────────────────────────────────────────────────────
# Bucket NAME is "ducky"; pub-be8cbd… is the public custom domain pointed at it,
# NOT the bucket name. Keep these separate.
DEFAULT_BUCKET = "ducky"
DEFAULT_PUBLIC_BASE = "https://pub-be8cbd81214d42469aa45b91d710100b.r2.dev"


def _read_aws_config_value(profile: str, key: str) -> str:
    """Read a key from the [profile <name>] section of ~/.aws/config."""
    cfg = Path.home() / ".aws" / "config"
    if not cfg.is_file():
        return ""
    section = f"[profile {profile}]"
    in_section = False
    for line in cfg.read_text().splitlines():
        line = line.strip()
        if line == section:
            in_section = True
            continue
        if line.startswith("[") and line.endswith("]"):
            in_section = False
        if in_section and "=" in line:
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip()
    return ""


def _resolve_endpoint() -> str:
    """R2 S3 endpoint: https://<account-id>.r2.cloudflarestorage.com."""
    acct = os.environ.get("R2_ACCOUNT_ID", "")
    if acct:
        return f"https://{acct}.r2.cloudflarestorage.com"
    # Read from ~/.aws/config (written by setup_r2.sh).
    endpoint = _read_aws_config_value("r2", "s3.endpoint_url")
    if endpoint:
        return endpoint
    return ""


ENDPOINT_URL = _resolve_endpoint()
BUCKET_NAME = os.environ.get("R2_BUCKET", DEFAULT_BUCKET)
# Public base is the custom domain, NOT {bucket}.r2.dev — keep them decoupled.
PUBLIC_BASE = os.environ.get("R2_PUBLIC_BASE", DEFAULT_PUBLIC_BASE)


# ── Credentials ─────────────────────────────────────────────────
def _read_credentials() -> tuple[str, str]:
    """Access key + secret, from env or ~/.aws/credentials [r2]."""
    ak = os.environ.get("R2_ACCESS_KEY_ID", "")
    sk = os.environ.get("R2_SECRET_ACCESS_KEY", "")
    if ak and sk:
        return ak, sk

    creds_file = Path.home() / ".aws" / "credentials"
    if not creds_file.is_file():
        print("ERROR: no R2 credentials found.", file=sys.stderr)
        print("   Set R2_ACCESS_KEY_ID + R2_SECRET_ACCESS_KEY, or run: bash setup_r2.sh",
              file=sys.stderr)
        sys.exit(1)

    in_section = False
    for line in creds_file.read_text().splitlines():
        line = line.strip()
        if line == "[r2]":
            in_section = True
        elif line.startswith("[") and line.endswith("]"):
            in_section = False
        elif in_section and "=" in line:
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k == "aws_access_key_id":
                ak = v
            elif k == "aws_secret_access_key":
                sk = v

    if not ak or not sk:
        print("ERROR: [r2] profile missing keys in ~/.aws/credentials", file=sys.stderr)
        print("   Re-run: bash setup_r2.sh", file=sys.stderr)
        sys.exit(1)
    return ak, sk


# ── AWS SigV4 signing ───────────────────────────────────────────
def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _signed_headers(
    method: str, url: str, body: bytes, content_type: str,
    access_key: str, secret_key: str,
    region: str = "auto", service: str = "s3",
    extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    parsed = urlparse(url)
    host = parsed.netloc
    path = parsed.path or "/"
    # Canonical query string: sorted, URI-encoded k=v pairs joined by &.
    if parsed.query:
        pairs = sorted(
            p.split("=", 1) if "=" in p else [p, ""]
            for p in parsed.query.split("&")
        )
        cq = "&".join(f"{k}={v}" for k, v in pairs)
    else:
        cq = ""

    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    canonical_values = {
        "content-type": content_type,
        "host": host,
        "x-amz-content-sha256": _sha256(body),
        "x-amz-date": amz_date,
    }
    for key, value in (extra_headers or {}).items():
        canonical_values[key.lower()] = " ".join(str(value).strip().split())
    canonical_headers = "".join(
        f"{key}:{canonical_values[key]}\n" for key in sorted(canonical_values)
    )
    signed_hdrs = ";".join(sorted(canonical_values))
    payload_hash = _sha256(body)

    canonical_request = (
        f"{method}\n{path}\n{cq}\n{canonical_headers}\n{signed_hdrs}\n{payload_hash}"
    )
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n"
        f"{_sha256(canonical_request.encode('utf-8'))}"
    )

    k_date = _sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    signing_key = _sign(k_service, "aws4_request")
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"AWS4-HMAC-SHA256 "
        f"Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_hdrs}, "
        f"Signature={signature}"
    )
    headers = {
        "Content-Type": content_type,
        "Host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
        "Authorization": authorization,
    }
    headers.update(extra_headers or {})
    return headers


def _content_type(name: str) -> str:
    guess, _ = mimetypes.guess_type(name)
    if guess:
        return guess
    return {
        ".whl": "application/x-wheel+zip",
        ".zip": "application/zip",
        ".json": "application/json",
        ".sh": "text/x-shellscript",
    }.get(Path(name).suffix, "application/octet-stream")


def upload_file(
    filepath: Path,
    s3_key: str,
    access_key: str,
    secret_key: str,
    dry_run: bool = False,
    cache_control: str = "",
) -> bool:
    body = filepath.read_bytes()
    url = f"{ENDPOINT_URL}/{BUCKET_NAME}/{s3_key}"
    content_type = _content_type(filepath.name)

    if dry_run:
        cache_note = f", {cache_control}" if cache_control else ""
        print(f"  [dry-run] {s3_key}  ({len(body)} bytes, {content_type}{cache_note})")
        return True

    extra_headers = {"Cache-Control": cache_control} if cache_control else None
    headers = _signed_headers(
        "PUT", url, body, content_type, access_key, secret_key,
        extra_headers=extra_headers,
    )
    req = Request(url, data=body, headers=headers, method="PUT")
    try:
        with urlopen(req, timeout=30) as resp:
            if resp.status in (200, 201):
                print(f"  OK   {s3_key}  ({len(body)} bytes)")
                return True
            print(f"  FAIL {s3_key}  — HTTP {resp.status}")
            return False
    except HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        print(f"  FAIL {s3_key}  — HTTP {e.code}: {detail}")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL {s3_key}  — {e}")
        return False


def _collect_files(
    root: Path,
    latest_wheel: Path | None = None,
    latest_archive: Path | None = None,
) -> list[Path]:
    """Collect files to upload, mirroring their role in the pipeline:

      - downloads/  → the latest student wheel and source archive from
                      `bash build_student.sh`
      - dist/latest.json → auto-update manifest consumed by self_update.py
      - student-install.sh (repo root) → `curl | bash` student entry point

    Two important invariants (these have broken releases before — don't regress):
      * The bucket NAME is "ducky"; pub-be8cbd… is the public custom domain, NOT the key prefix.
      * Filenames use the "ducky_student-" prefix (underscore), matching build_student.sh DIST_NAME.
    """
    files: list[Path] = []
    seen: set[str] = set()

    def add(p: Path) -> None:
        if p.is_file() and p.name not in seen:
            files.append(p)
            seen.add(p.name)

    downloads_dir = root / "downloads"
    if latest_wheel and latest_archive:
        add(latest_wheel)
        add(latest_archive)
    elif downloads_dir.is_dir():
        print(
            "WARN: latest artifacts were not selected; run the build before uploading.",
            file=sys.stderr,
        )
    else:
        print(f"WARN: {downloads_dir} not found — run `bash build_student.sh` first.",
              file=sys.stderr)

    # latest.json is the generated auto-update manifest; keep it under dist/.
    add(root / "dist" / "latest.json")

    installer = root / "student-install.sh"
    if not installer.is_file():
        print("WARN: student-install.sh not found at repo root.", file=sys.stderr)
    add(installer)

    return files


def _validate_student_archive(archive: Path) -> None:
    """Reject an archive that is incomplete, unsafe, or a teacher build."""
    forbidden = {
        "ducky/server.py",
        "ducky/classroom.py",
        "ducky/teacher_workspace.py",
        "ducky/teacher_backend.py",
        "ducky/lesson_builder.py",
        "ducky/validator.py",
        "ducky/authoring.py",
        "ducky/gui/teacher.py",
        "ducky/gui/teacher_gui.py",
        "ducky/cli/teacher_commands.py",
        "ducky/cli/practice.py",
    }
    try:
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            if "pyproject.toml" not in names:
                raise SystemExit("ERROR: student archive is missing pyproject.toml")
            init_names = [
                name
                for name in names
                if name == "src/ducky/__init__.py"
                or name.endswith("/src/ducky/__init__.py")
            ]
            if not init_names:
                raise SystemExit("ERROR: student archive is missing src/ducky/__init__.py")
            unsafe: list[str] = []
            teacher_names: list[str] = []
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                parts = Path(name).parts
                if name.startswith("/") or ".." in parts:
                    unsafe.append(name)
                if any(name.endswith(path) for path in forbidden):
                    teacher_names.append(name)
            if unsafe:
                raise SystemExit(f"ERROR: student archive contains unsafe paths: {unsafe[:3]}")
            if teacher_names:
                raise SystemExit(
                    "ERROR: student archive contains teacher-only modules: "
                    + ", ".join(teacher_names[:3])
                )
    except zipfile.BadZipFile as exc:
        raise SystemExit(f"ERROR: invalid student archive {archive.name}: {exc}") from exc


def _validate_release_manifest(
    manifest: dict,
    wheel: Path,
    archive: Path,
    expected_version: str,
    public_base: str,
) -> None:
    """Validate that metadata, filenames, checksums, and URLs agree."""
    if manifest.get("build_type") != "student":
        raise SystemExit("ERROR: release manifest is not a student release")
    if str(manifest.get("version", "")) != expected_version:
        raise SystemExit(
            f"ERROR: release manifest version {manifest.get('version')!r} "
            f"does not match artifacts v{expected_version}"
        )
    public_base = public_base.rstrip("/")
    zip_url = str(manifest.get("download_url", ""))
    wheel_url = str(manifest.get("wheel_url", ""))
    expected_zip_suffix = f"/{archive.name}"
    expected_wheel_suffix = f"/{wheel.name}"
    if (
        not zip_url.startswith(public_base + "/releases/")
        or not zip_url.endswith(expected_zip_suffix)
        or "/student-latest." in zip_url
    ):
        raise SystemExit(
            "ERROR: manifest download_url must point to a unique immutable release zip"
        )
    if manifest.get("versioned_zip_url") != zip_url:
        raise SystemExit("ERROR: manifest versioned_zip_url does not match the archive")
    if (
        not wheel_url.startswith(public_base + "/releases/")
        or not wheel_url.endswith(expected_wheel_suffix)
        or "/student-latest." in wheel_url
    ):
        raise SystemExit(
            "ERROR: manifest wheel_url must point to a unique immutable release wheel"
        )
    if manifest.get("versioned_wheel_url") != wheel_url:
        raise SystemExit("ERROR: manifest versioned_wheel_url does not match the wheel")
    if zip_url.rsplit("/", 1)[0] != wheel_url.rsplit("/", 1)[0]:
        raise SystemExit("ERROR: student wheel and zip must share one release prefix")
    archive_sha = _sha256(archive.read_bytes())
    wheel_sha = _sha256(wheel.read_bytes())
    if manifest.get("sha256") != archive_sha:
        raise SystemExit("ERROR: manifest sha256 does not match the student zip")
    if manifest.get("wheel_sha256") != wheel_sha:
        raise SystemExit("ERROR: manifest wheel_sha256 does not match the student wheel")
    _validate_student_archive(archive)


def _release_key(manifest: dict, public_base: str, artifact_name: str) -> str:
    """Convert a validated immutable manifest URL into its R2 object key."""
    url = str(
        manifest.get("versioned_zip_url" if artifact_name.endswith(".zip") else "versioned_wheel_url", "")
    )
    prefix = public_base.rstrip("/") + "/"
    if not url.startswith(prefix):
        raise SystemExit(f"ERROR: release URL is outside the configured public base: {url}")
    return url[len(prefix):]


def _student_artifact_version(path: Path) -> tuple[int, ...]:
    """Return the numeric version embedded in a student artifact filename."""
    match = re.search(r"ducky_student-v?(\d+(?:\.\d+){1,3})", path.name)
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def _latest_student_artifacts(root: Path) -> tuple[Path, Path]:
    """Find the highest-version student wheel and source archive."""
    downloads = root / "downloads"
    wheels = sorted(downloads.glob("ducky_student-*.whl"), key=_student_artifact_version)
    archives = sorted(downloads.glob("ducky_student-v*.zip"), key=_student_artifact_version)
    if not wheels or not archives:
        raise SystemExit(
            "ERROR: expected a student wheel and zip in downloads/. "
            "Run `bash build_student.sh` first."
        )
    wheel = wheels[-1]
    archive = archives[-1]
    wheel_version = _student_artifact_version(wheel)
    archive_version = _student_artifact_version(archive)
    if wheel_version != archive_version:
        raise SystemExit(
            f"ERROR: student wheel/archive versions differ: {wheel.name} vs {archive.name}"
        )
    return wheel, archive


def check_access() -> None:
    """Non-destructive signed LIST (max-keys=1) to verify the token works."""
    if not ENDPOINT_URL:
        print("ERROR: R2 endpoint unknown. Set R2_ACCOUNT_ID or run: bash setup_r2.sh",
              file=sys.stderr)
        sys.exit(1)
    access_key, secret_key = _read_credentials()
    url = f"{ENDPOINT_URL}/{BUCKET_NAME}/?list-type=2&max-keys=1"
    headers = _signed_headers("GET", url, b"", "application/xml", access_key, secret_key)
    # GET has no body; drop content-type from headers (kept above for signing only works fine).
    req = Request(url, headers=headers, method="GET")
    try:
        with urlopen(req, timeout=30) as resp:
            body = resp.read().decode(errors="replace")
            n = body.count("<Key>")
            print(f"  OK   HTTP {resp.status} — token works. Bucket listing returned {n} key(s).")
            print(f"  bucket={BUCKET_NAME}  endpoint={ENDPOINT_URL}")
            sys.exit(0)
    except HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        print(f"  FAIL HTTP {e.code}: {detail}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL {e}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    check = "--check" in sys.argv

    if check:
        check_access()  # exits

    if not dry_run and not ENDPOINT_URL:
        print("ERROR: R2 endpoint unknown. Set R2_ACCOUNT_ID or run: bash setup_r2.sh",
              file=sys.stderr)
        sys.exit(1)

    root = Path(__file__).resolve().parent
    latest_wheel, latest_archive = _latest_student_artifacts(root)
    files = _collect_files(root, latest_wheel, latest_archive)
    if not files:
        print("No files to upload. Run `bash build_student.sh` first.", file=sys.stderr)
        sys.exit(1)

    latest_manifest = root / "dist" / "latest.json"
    if not latest_manifest.is_file():
        raise SystemExit("ERROR: dist/latest.json is missing. Run `bash build_student.sh` first.")
    try:
        manifest = json.loads(latest_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"ERROR: invalid dist/latest.json: {exc}") from exc
    expected_version = ".".join(str(part) for part in _student_artifact_version(latest_archive))
    _validate_release_manifest(
        manifest,
        latest_wheel,
        latest_archive,
        expected_version,
        PUBLIC_BASE,
    )

    if dry_run:
        access_key, secret_key = "", ""
    else:
        access_key, secret_key = _read_credentials()

    print()
    print(f"  R2 upload  {'(dry-run)' if dry_run else ''}")
    print(f"  bucket:   {BUCKET_NAME}")
    print(f"  endpoint: {ENDPOINT_URL}")
    installer = root / "student-install.sh"
    targets = [
        # Publish immutable objects first. A failed artifact upload must never
        # be followed by a manifest that points at it.
        (latest_archive, _release_key(manifest, PUBLIC_BASE, latest_archive.name)),
        (latest_wheel, _release_key(manifest, PUBLIC_BASE, latest_wheel.name)),
    ]
    if installer.is_file():
        targets.append((installer, "student-install.sh"))
    # Stable aliases are convenience links only; the manifest never depends on
    # them, so a stale CDN alias cannot make a verified release inconsistent.
    targets.extend(
        [
            (latest_archive, "student-latest.zip"),
            (latest_wheel, "student-latest.whl"),
        ]
    )
    # Publish latest.json last. R2 has no multi-object transaction, so this
    # ordering keeps students on the previous complete release if anything
    # before the manifest fails.
    targets.append((latest_manifest, "latest.json"))
    print(f"  files:    {len(targets)}")
    print()

    ok = 0
    for f, key in targets:
        if not upload_file(
            f,
            key,
            access_key,
            secret_key,
            dry_run=dry_run,
            cache_control=(
                "no-store, max-age=0"
                if key in {"latest.json", "student-install.sh"}
                else "public, max-age=60, must-revalidate"
                if key in {"student-latest.zip", "student-latest.whl"}
                else "public, max-age=31536000, immutable"
            ),
        ):
            print(
                "  Aborting before publishing any later release metadata. "
                "Students will remain on the previous manifest."
            )
            sys.exit(1)
        ok += 1

    print()
    print(f"  Done: {ok}/{len(targets)} uploaded.")
    print(f"  Public base: {PUBLIC_BASE}/")
    for _, key in targets:
        print(f"    {PUBLIC_BASE}/{key}")
    if ok < len(targets):
        sys.exit(1)


if __name__ == "__main__":
    main()
