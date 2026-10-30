"""Run NetOrbit: ``python -m netorbit_pi`` or the ``netorbit-pi`` command."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
import signal
import tempfile

from .const import DEFAULT_PORT, VERSION

_LOGGER = logging.getLogger("netorbit_pi")
SYSTEM_DATA_DIR = "/var/lib/netorbit"


def default_data_dir() -> Path:
    if os.environ.get("NETORBIT_DATA"):
        return Path(os.environ["NETORBIT_DATA"])
    if os.environ.get("STATE_DIRECTORY"):  # set by systemd's StateDirectory=
        return Path(os.environ["STATE_DIRECTORY"])
    if Path(SYSTEM_DATA_DIR).is_dir() and os.access(SYSTEM_DATA_DIR, os.W_OK):
        return Path(SYSTEM_DATA_DIR)  # the installed service's data, e.g. for --reset-password
    return Path.home() / ".local" / "share" / "netorbit"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="netorbit-pi", description="A live radial map of your home network.")
    p.add_argument("--host", default=os.environ.get("NETORBIT_HOST", "0.0.0.0"), help="address to listen on (default all)")
    p.add_argument("--port", type=int, default=int(os.environ.get("NETORBIT_PORT", DEFAULT_PORT)), help=f"web port (default {DEFAULT_PORT})")
    p.add_argument("--data-dir", type=Path, default=None, help="where settings and history are kept")
    p.add_argument("--demo", nargs="?", const="snmp_chain", metavar="SHAPE",
                   help="run against a simulated network instead of the real one "
                        "(shapes: flat, mesh_no_data, unifi, snmp_single, snmp_chain, snmp_router)")
    p.add_argument("--reset-password", action="store_true", help="remove the admin password and exit")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("--version", action="version", version=f"netorbit-pi {VERSION}")
    return p.parse_args(argv)


async def _serve(args: argparse.Namespace) -> None:
    from .app import App  # noqa: PLC0415

    if args.demo:
        from .const import CONF_GATEWAY, CONF_PING_INTERVAL, CONF_SUBNET  # noqa: PLC0415
        from .sim import SimNetIO  # noqa: PLC0415
        from .store import ConfigStore  # noqa: PLC0415

        net = SimNetIO.from_shape(args.demo, seed=7, churn=0.04)
        data_dir = args.data_dir or Path(tempfile.mkdtemp(prefix="netorbit-demo-"))
        cfg = ConfigStore(data_dir / "config.json")
        if not cfg.configured:
            cfg.update(data={CONF_SUBNET: net.net.subnet, CONF_GATEWAY: net.net.gateway},
                       options={CONF_PING_INTERVAL: 10})
        app = App(data_dir, net, demo=True)
    else:
        from .netio import SystemNetIO  # noqa: PLC0415

        app = App(args.data_dir or default_data_dir(), SystemNetIO())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await app.start(args.host, args.port)
    for sock in app.server.sockets:
        host, port = sock.getsockname()[:2]
        _LOGGER.info("NetOrbit %s%s listening on http://%s:%s (data in %s)", VERSION,
                     " demo" if args.demo else "", host, port, app.data_dir)
    await stop.wait()
    _LOGGER.info("Stopping")
    await app.stop()


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose:
        for noisy in ("pysnmp", "icmplib", "asyncio"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.reset_password:
        from .store import ConfigStore  # noqa: PLC0415

        data_dir = args.data_dir or default_data_dir()
        path = data_dir / "config.json"
        cfg = ConfigStore(path)
        cfg.data.pop("password_hash", None)
        cfg.save()
        try:
            # Run as root for the service's data: hand the file back to the service user.
            st = data_dir.stat()
            os.chown(path, st.st_uid, st.st_gid)
        except OSError:
            pass
        print(f"Admin password removed from {path}. Restart NetOrbit.")
        return
    asyncio.run(_serve(args))


if __name__ == "__main__":
    main()
