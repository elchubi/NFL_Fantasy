"""Start the server on the port and interface the host platform expects.

Two things differ between platforms and both fail confusingly if guessed wrong:

  PORT   Railway assigns one and injects it. Hardcoding a port means the
         deployment builds, starts, and then fails its healthcheck forever.

  HOST   Railway's private network (`<service>.railway.internal`) is IPv6-only,
         so a process bound to 0.0.0.0 is unreachable from a sibling service -
         the caller just times out with nothing in either log. Binding `::`
         covers both IPv6 and IPv4 on a dual-stack host. Some local Docker
         setups have IPv6 disabled entirely, where binding `::` raises instead,
         so that case falls back rather than refusing to boot.

Set HOST explicitly to override the detection.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess

import uvicorn

log = logging.getLogger("entrypoint")


def drop_privileges(data_dir: str = "/data") -> None:
    """Fix ownership of the mounted data directory, then drop from root to
    the unprivileged `appuser` the image creates, before anything serves.

    The Dockerfile's own build-time `chown` only affects what is baked into
    the image layer. A Railway Volume (or any external volume) mounts over
    that path at container start as a separate filesystem - typically
    root-owned - which silently shadows the build-time chown and leaves
    `appuser` unable to write the SQLite file at all ("unable to open
    database file"). Fixing it here, after the mount but before serving,
    works regardless of what ownership the volume arrives with; running as
    root only for this one step, never for the app itself.
    """
    if os.name != "posix" or os.getuid() != 0:
        return
    import pwd

    try:
        user = pwd.getpwnam("appuser")
    except KeyError:
        log.warning("No 'appuser' in this image; staying root.")
        return

    if os.path.isdir(data_dir):
        subprocess.run(
            ["chown", "-R", f"{user.pw_uid}:{user.pw_gid}", data_dir], check=False
        )
    os.setgid(user.pw_gid)
    os.setuid(user.pw_uid)


def pick_host() -> str:
    explicit = os.getenv("HOST", "").strip()
    if explicit:
        return explicit
    try:
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            probe.bind(("::", 0))
        finally:
            probe.close()
    except OSError:
        # No usable IPv6 stack; private networking would not work here anyway.
        return "0.0.0.0"
    return "::"


def main(app: str, default_port: int) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    drop_privileges()
    host = pick_host()
    port = int(os.getenv("PORT", str(default_port)))
    log.info("Starting %s on [%s]:%s", app, host, port)
    uvicorn.run(app, host=host, port=port, workers=1)


if __name__ == "__main__":
    main(os.getenv("APP_MODULE", "main:app"), int(os.getenv("DEFAULT_PORT", "8000")))
