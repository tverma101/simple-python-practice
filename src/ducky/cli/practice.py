"""Launch and manage the practice server from the command line.

The server existed with no way to start it. ``ducky`` opened a Tkinter TUI, and
the subcommand list in ``cli/main.py`` had no ``serve``, so a student following
the README had no command that produced a page. This adds it, and the supporting
commands to get a usable install going: create a class, issue per-student access
codes, build the content store.

The server binds ``0.0.0.0`` by default so anyone on the local network can open
it. That is the requested behaviour and it is a real exposure, so the banner
prints the addresses a student should share, the security posture in one line,
and a pointer to ``docs/SECURITY_MODEL.md``. Read that before putting this on a
network you do not control.
"""

from __future__ import annotations

import argparse
import socket
import sys
import webbrowser
from pathlib import Path

__all__ = ["add_parser", "run", "default_content_db", "default_state_dir", "local_addresses"]


def default_content_db() -> Path:
    from ..paths import app_dir

    return app_dir() / "content" / "content.db"


def default_state_dir() -> Path:
    from ..paths import app_dir

    return app_dir() / "content"


def local_addresses(port: int) -> list[str]:
    """Every address a student could type, best-effort."""
    found = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            address = info[4][0]
            if not address.startswith("127."):
                found.add(address)
    except OSError:
        pass
    return [f"http://{address}:{port}" for address in sorted(found)]


def _load_store(path: Path, *, required: bool = True):
    from ..contentstore import ContentStore

    if not path.exists():
        if required:
            raise SystemExit(
                f"no content database at {path}\n"
                f"Build one first:\n"
                f"  python tools/build_content.py --db <coding_platform.db> "
                f"--out {path}"
            )
        return None
    return ContentStore(path, read_only=True)


def add_parser(subparsers) -> None:
    """Register ``ducky web`` and its helpers."""
    parser = subparsers.add_parser(
        "web",
        help="serve the practice site on this machine and the local network",
        description=__doc__,
    )
    parser.add_argument(
        "--host", default="0.0.0.0",
        help="interface to bind; 0.0.0.0 makes it reachable from the LAN",
    )
    parser.add_argument("--port", type=int, default=8765, help="port to listen on")
    parser.add_argument(
        "--content", default=None,
        help=f"path to the content database (default {default_content_db()})",
    )
    parser.add_argument(
        "--state", default=None,
        help=f"where to keep drafts and the run log (default {default_state_dir()})",
    )
    parser.add_argument(
        "--classroom", default=None,
        help="classroom database holding access codes (default the app directory). "
             "Must be the same file `ducky access-codes` writes to, or no code "
             "will be accepted.",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="do not open a browser on start",
    )
    parser.add_argument(
        "--allow-lan", action="store_true",
        help="required to bind 0.0.0.0; makes the LAN exposure explicit",
    )

    codes = subparsers.add_parser(
        "access-codes",
        help="issue per-student access codes for a class",
        description=(
            "Per-student codes, not one shared code: the practice server runs "
            "student code on request, so 'who ran this and how much CPU did they "
            "burn' has to be answerable, and one shared code makes both "
            "unanswerable while letting a single machine saturate the grader."
        ),
    )
    codes.add_argument("class_id", help="the class to add students to")
    codes.add_argument("names", nargs="+", help="student names")
    codes.add_argument(
        "--class-name", default=None,
        help="create the class with this name if it does not exist",
    )
    codes.add_argument(
        "--db", default=None, help="classroom database (default the app directory)",
    )


def run(args: argparse.Namespace) -> int:
    if getattr(args, "class_id", None):
        return _issue_codes(args)
    return _serve(args)


# ── serve ───────────────────────────────────────────────────────────


def _serve(args: argparse.Namespace) -> int:
    from ..classroom import ClassroomDB
    from ..routes import build_app
    from ..webapp import serve

    content_path = Path(args.content) if args.content else default_content_db()
    state_dir = Path(args.state) if args.state else default_state_dir()

    store = _load_store(content_path)
    # Must be the same database `ducky access-codes` writes, or every code is
    # rejected and the site looks broken for a reason that is not visible.
    classroom = ClassroomDB(Path(args.classroom) if args.classroom else None)
    app = build_app(content=store, classroom=classroom)
    app.attach_stores(state_dir)

    lan = args.host not in ("127.0.0.1", "localhost", "::1")
    if lan and not args.allow_lan:
        # Explicit, not a warning nobody reads. Binding 0.0.0.0 means any machine
        # on the network can reach a service that executes code.
        print(
            "Refusing to bind 0.0.0.0 without --allow-lan.\n"
            "\n"
            "That makes this reachable by every device on the network, and it\n"
            "runs student code on request. If that is what you want:\n"
            "\n"
            "  ducky web --allow-lan\n"
            "\n"
            "For just this machine, use --host 127.0.0.1\n"
            "Read docs/SECURITY_MODEL.md first.",
            file=sys.stderr,
        )
        return 2

    try:
        server = serve(app, host=args.host, port=args.port)
    except OSError as exc:
        print(f"could not bind {args.host}:{args.port} -- {exc}", file=sys.stderr)
        return 1

    port = server.server_address[1]
    urls = local_addresses(port)
    print("Simple Python Practice  v0.5.2")
    print(f"  {store.count():,} verified problems")
    print()
    print("  Open:")
    for url in urls:
        print(f"    {url}")
    print()
    print("  Before anyone can run code they need an access code. Issue them with:")
    print("    ducky access-codes <class-id> <name> [<name> ...] --class-name \"<name>\"")
    print()
    if lan:
        print("  Bound to the LAN. Anyone here can browse; running code needs a code.")
    print("  Ctrl-C to stop.")
    try:
        if not args.no_browser:
            webbrowser.open(urls[0])
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.shutdown()
        for closer in (store, app.classroom):
            if closer is not None and hasattr(closer, "close"):
                closer.close()
    return 0


# ── access codes ─────────────────────────────────────────────────────


def _issue_codes(args: argparse.Namespace) -> int:
    from ..classroom import ClassroomDB

    db = ClassroomDB(Path(args.db) if args.db else None)
    if db.get_class(args.class_id) is None:
        if not args.class_name:
            print(
                f"no class {args.class_id!r}. Pass --class-name to create it.",
                file=sys.stderr,
            )
            return 2
        created = db.create_class(args.class_id, args.class_name)
        print(f"created class {created['class_id']}  (class code {created['access_code']})")

    for name in args.names:
        record = db.create_student(args.class_id, name)
        print(f"  {name:<20} {record['access_code']}")

    print()
    print("Hand these out individually. A student signs in at / and can then run code.")
    print("These are secrets: anyone holding one can run code on this machine.")
    return 0
