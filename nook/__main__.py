"""Command line.

    python -m nook run     --config /etc/nook/config.toml   # the service
    python -m nook doors   --config ...                     # print door states
    python -m nook unlock 17 --config ...                   # maintenance unlock
    python -m nook sim                                      # simulator, no hardware
    python -m nook sign-command 17 --config ...             # cloud-side helpers
    python -m nook issue-token 17 --member m-42 --config ...
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
import uuid

from . import __version__, config as config_mod
from .app import build_controller, build_locks
from .bus import open_serial
from .link import MqttLink
from .power import PiPowerMonitor
from .security import issue_offline_token, sign_command


def cmd_run(args) -> int:
    cfg = config_mod.load(args.config)
    m = cfg.mqtt
    link = MqttLink(cfg.cabinet.id, m.host, m.port, m.tls, m.ca or None, m.cert or None,
                    m.key or None, m.username or None, m.password or None)
    power = PiPowerMonitor(cfg.power.mains_gpio, cfg.power.ina219_address, cfg.power.i2c_bus,
                           cfg.power.battery_empty_v, cfg.power.battery_full_v)
    transport = open_serial(cfg.serial.port, cfg.serial.baudrate, cfg.serial.timeout)
    ctl = build_controller(cfg, transport, link, power, cfg.secret())
    ble = None
    if cfg.ble.enabled:
        try:
            import bless  # noqa: F401
            from .ble import BleServer
            ble = BleServer(cfg.ble.name, ctl.handle_offline_token)
        except ImportError:
            logging.warning("Bluetooth fallback disabled: pip install bless")
    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    ctl.start()
    if ble:
        ble.start()
    done.wait()
    logging.info("stopping")
    if ble:
        ble.stop()
    ctl.stop()
    return 0


def cmd_doors(args) -> int:
    cfg = config_mod.load(args.config)
    locks = build_locks(cfg, open_serial(cfg.serial.port, cfg.serial.baudrate, cfg.serial.timeout))
    doors, failed = locks.read_all()
    opened = [k for k, v in sorted(doors.items()) if v]
    print(f"open doors: {opened or 'none'}")
    if failed:
        print(f"boards not answering: {failed}")
    return 1 if failed else 0


def cmd_unlock(args) -> int:
    cfg = config_mod.load(args.config)
    locks = build_locks(cfg, open_serial(cfg.serial.port, cfg.serial.baudrate, cfg.serial.timeout))
    res = locks.unlock(args.locker)
    print(json.dumps(res.__dict__))
    return 0 if res.ok else 1


def cmd_sign(args) -> int:
    cfg = config_mod.load(args.config)
    cmd = sign_command(cfg.secret(), cfg.cabinet.id, args.locker, str(uuid.uuid4()),
                       int(time.time()) + args.ttl, args.member)
    print(json.dumps(cmd))
    return 0


def cmd_token(args) -> int:
    cfg = config_mod.load(args.config)
    now = int(time.time())
    print(issue_offline_token(cfg.secret(), cfg.cabinet.id, args.locker, args.member,
                              now, now + args.hours * 3600, uuid.uuid4().hex))
    return 0


def cmd_sim(args) -> int:
    from .sim.runner import run_sim
    return run_sim(args)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="nook", description=f"Nook Locker controller {__version__}")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def with_config(p):
        p.add_argument("--config", "-c", default="/etc/nook/config.toml")
        return p

    with_config(sub.add_parser("run", help="run the cabinet service")).set_defaults(fn=cmd_run)
    with_config(sub.add_parser("doors", help="print door states")).set_defaults(fn=cmd_doors)
    p = with_config(sub.add_parser("unlock", help="maintenance unlock, bypasses the cloud"))
    p.add_argument("locker", type=int)
    p.set_defaults(fn=cmd_unlock)
    p = with_config(sub.add_parser("sign-command", help="cloud helper: a signed open command"))
    p.add_argument("locker", type=int)
    p.add_argument("--member")
    p.add_argument("--ttl", type=int, default=30)
    p.set_defaults(fn=cmd_sign)
    p = with_config(sub.add_parser("issue-token", help="cloud helper: an offline Bluetooth token"))
    p.add_argument("locker", type=int)
    p.add_argument("--member", required=True)
    p.add_argument("--hours", type=int, default=24)
    p.set_defaults(fn=cmd_token)
    p = sub.add_parser("sim", help="simulated cabinet with a web dashboard, no hardware")
    p.add_argument("--http", default="127.0.0.1:8080", help="dashboard address")
    p.add_argument("--mqtt", help="use a real broker, e.g. localhost:1883 (default: in-memory)")
    p.add_argument("--pty", action="store_true",
                   help="Linux/macOS: run the boards behind a virtual serial port")
    p.add_argument("--auto-close", type=float, default=0,
                   help="close opened doors automatically after N seconds")
    p.add_argument("--db", default="", help="journal file (default: temporary)")
    p.set_defaults(fn=cmd_sim)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        return args.fn(args)
    except (OSError, ValueError) as exc:
        logging.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
