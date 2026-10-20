"""The ``threadfin`` command (port of ``threadfin.go``): start the server, show system info, or restore
a backup.

    threadfin [-config DIR] [-port PORT] [-bind IP] [-debug 0-3]
    threadfin -restore /path/to/threadfin_backup.zip
    threadfin -info

Go's single-dash flags work, and so do ``--config`` etc. ``-port`` is saved to settings.json, as in
Go. Differences: with no ``-bind`` and no ``bindIpAddress``, the server listens on ``127.0.0.1``
unless web authentication is already on, in which case it still listens on all interfaces (Go
listened only on the first LAN address it found, and the setting won over the flag). ``-bind``
wins over the setting. ``-dev`` is gone; ``-branch`` is accepted and saved but nothing updates
itself.
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import socket
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO, override

from . import __version__
from .backup import BackupError, restore_backup
from .imagecache import local_address
from .redact import RedactAccessLog
from .settings import Settings, SettingsError, load_settings
from .system import NAME, SystemInfo, default_config_folder

log = logging.getLogger("threadfin")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="threadfin", description=f"{NAME} {__version__}: M3U proxy and EPG mapper "
                                "for Plex / Emby / Jellyfin.")  # fmt: skip
    p.add_argument(
        "-config", "--config", metavar="DIR", default="", help=f"config folder (default: {default_config_folder()})"
    )
    p.add_argument("-port", "--port", default="", help="server port, saved to settings.json (default: 34400)")
    p.add_argument(
        "-bind",
        "--bind",
        default="",
        metavar="IP",
        help="address to listen on (default: bindIpAddress, else all interfaces when web "
        "authentication is on, else 127.0.0.1)",
    )
    p.add_argument("-restore", "--restore", metavar="FILE", default="", help="restore a backup, then exit")
    p.add_argument("-info", "--info", action="store_true", help="show system info, then exit")
    p.add_argument(
        "-debug",
        "--debug",
        type=int,
        default=0,
        choices=range(4),
        metavar="0-3",
        help="debug level (2 and up also logs every request)",
    )
    p.add_argument("-branch", "--branch", default="", help=argparse.SUPPRESS)  # Go's self-update branch
    p.add_argument("-version", "--version", action="version", version=f"{NAME} {__version__}")
    return p


def setup_logging(debug: int) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactAccessLog) for f in access_logger.filters):
        access_logger.addFilter(RedactAccessLog())


def prepare(config: str) -> SystemInfo:
    """Config, data, backup, cache and temp folders and the system files (Go ``Init``)."""
    system = SystemInfo(config=Path(config)) if config else SystemInfo()
    system.create_folders()
    system.create_files()
    return system


def listening_on_loopback(host: str) -> bool:
    """True for ``localhost`` and loopback IPs. ``0.0.0.0`` and ``::`` are not loopback."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def listen_address(flag: str, settings: Settings) -> str:
    """``-bind``, else ``bindIpAddress``, else a safe default.

    An install that has not chosen an address and has not turned on web authentication listens
    on ``127.0.0.1`` only, so the admin UI and API are not reachable from other machines. A saved
    ``bindIpAddress`` (including ``0.0.0.0``) or ``-bind`` is used as given. Web authentication
    already on, with no address set, still listens on all interfaces so that login keeps working
    from the network.
    """
    if flag:
        return flag
    if settings.bind_ip_address:
        return settings.bind_ip_address
    if settings.authentication_web:
        return "0.0.0.0"
    return "127.0.0.1"


def port_number(settings: Settings) -> int:
    try:
        port = int(settings.port)
    except ValueError:
        raise SettingsError(f"invalid port {settings.port!r} in settings.json") from None
    if not 0 < port < 65536:
        raise SettingsError(f"invalid port {port}")
    return port


def show_info(system: SystemInfo, settings: Settings, out: TextIO) -> None:
    """Go ``ShowSystemInfo`` (the parts that exist here)."""
    files = settings.files
    rows = [
        ("Version", f"{NAME} {__version__} (settings format {system.db_version})"),
        ("UUID", settings.uuid),
        ("Folder (config)", str(system.config)),
        ("Folder (data)", str(system.folder_data)),
        ("Folder (backup)", settings.backup_path),
        ("Folder (temp)", str(system.folder_temp)),
        ("Port", settings.port),
        ("EPG source", settings.epg_source),
        ("Tuner", str(settings.tuner)),
        ("Playlists (M3U)", str(len(files.m3u))),
        ("Guides (XMLTV)", str(len(files.xmltv))),
        ("Update schedule", ", ".join(settings.update) or "none"),
        ("Files update", str(settings.files_update).lower()),
        ("Backup keep", str(settings.backup_keep)),
        ("Image caching", str(settings.cache_images).lower()),
        ("Web authentication", str(settings.authentication_web).lower()),
        ("API", str(settings.api).lower()),
    ]
    width = max(len(k) for k, _ in rows) + 1
    for key, value in rows:
        print(f"{key + ':':<{width}} {value}", file=out)


def confirm_restore(archive: str, system: SystemInfo, ask: Callable[[str], str], out: TextIO) -> int:
    """Go ``ThreadfinRestoreFromCLI``: show what will happen, ask, restore. Returns the exit code."""
    print(f"Version:       {NAME} {__version__}", file=out)
    print(f"Backup file:   {archive}", file=out)
    print(f"System folder: {system.config}", file=out)
    print(file=out)
    try:
        answer = ask("All data will be replaced with those from the backup. Should the files be restored? [yes|no]:")
    except EOFError:
        answer = ""
    answer = answer.strip().lower()
    if answer == "no":
        return 0
    if answer != "yes":
        print("Invalid input", file=out)
        return 1
    current = load_settings(system)
    try:
        result = restore_backup(system, archive, current.port)
    except BackupError as exc:
        print(f"Restore failed: {exc}", file=out)
        return 1
    print(f"Backup was successfully restored. {NAME} can now be started normally.", file=out)
    if result.port_changed:
        print(f"The port is now {result.new_port}.", file=out)
    return 0


def serve(system: SystemInfo, settings: Settings, bind: str, debug: int) -> None:  # pragma: no cover - runs uvicorn
    import uvicorn

    from .service import Service
    from .web import create_app

    host, port = listen_address(bind, settings), port_number(settings)
    shown = local_address() if host in ("0.0.0.0", "::") else host
    log.info("%s %s, config folder %s", NAME, __version__, system.config)

    class Server(uvicorn.Server):
        @override
        async def startup(self, sockets: list[socket.socket] | None = None) -> None:
            await super().startup(sockets)  # the app's start-up (update / build), then the port is bound
            if self.started:
                log.info("Web interface: http://%s:%d/web/", shown, port)

    app = create_app(Service(system, settings))
    config = uvicorn.Config(app, host=host, port=port, log_level="debug" if debug >= 3 else "info" if debug else "warning",
                            access_log=debug >= 2, log_config=None)  # fmt: skip
    Server(config).run()


def main(argv: Sequence[str] | None = None, *, ask: Callable[[str], str] = input, out: TextIO = sys.stdout,
         run: Callable[[SystemInfo, Settings, str, int], None] = serve) -> int:  # fmt: skip
    args = parser().parse_args(argv)
    setup_logging(args.debug)
    try:
        system = prepare(args.config)
        if args.restore:
            return confirm_restore(args.restore, system, ask, out)
        settings = load_settings(system, flag_port=args.port, flag_branch=args.branch)
        if args.info:
            show_info(system, settings, out)
            return 0
        port_number(settings)
        host = listen_address(args.bind, settings)
        if not listening_on_loopback(host) and not settings.authentication_web:
            log.warning(
                "authentication.web is off and %s is listening on %s, so the admin UI and API are "
                "reachable without a login. Turn on web authentication, or listen on 127.0.0.1.",
                NAME,
                host,
            )
        run(system, settings, args.bind, args.debug)
    except (SettingsError, OSError) as exc:
        print(f"{NAME}: {exc}", file=sys.stderr)
        return 1
    return 0


__all__ = ["listen_address", "listening_on_loopback", "main", "parser", "port_number"]
