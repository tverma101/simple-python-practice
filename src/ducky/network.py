"""Network utilities for Ducky."""

from __future__ import annotations

import json
import socket
import time

DISCOVERY_PORT = 8421
DISCOVERY_MESSAGE = b"DUCKY_DISCOVER_V1"


def get_local_ip() -> str:
    """Return the LAN IP address of this machine.

    Uses a UDP socket trick to determine the outbound IP without making
    any actual network connections. Falls back to 127.0.0.1 if detection fails.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            # Connect to a public DNS server (no actual packet sent)
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def get_local_ip_url(port: int) -> str:
    """Return the full server URL with the detected local IP.

    Args:
        port: The port number the server is listening on.

    Returns:
        A URL string like 'http://192.168.1.100:8420'.
    """
    ip = get_local_ip()
    return f"http://{ip}:{port}"


def discover_teacher_servers(*, port: int = 8420, timeout: float = 1.5) -> list[dict[str, object]]:
    """Find teacher servers on the current LAN without broadcasting secrets."""
    found: dict[str, dict[str, object]] = {}
    deadline = time.monotonic() + max(0.1, timeout)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(0.2)
            sock.bind(("", 0))
            sock.sendto(DISCOVERY_MESSAGE + b":" + str(port).encode(), ("<broadcast>", DISCOVERY_PORT))
            while time.monotonic() < deadline:
                try:
                    payload, address = sock.recvfrom(4096)
                except TimeoutError:
                    continue
                try:
                    data = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                url = str(data.get("url", "")).strip()
                if url:
                    data["url"] = url
                    data["host"] = address[0]
                    found[url] = data
    except OSError:
        return []
    return list(found.values())
