"""`python -m nook sim`: the real controller, wired to simulated hardware."""

from __future__ import annotations

import collections
import json
import logging
import os
import secrets
import tempfile
import threading
import time
import uuid

from .. import config as config_mod
from ..app import build_controller
from ..link import LoopbackLink, MqttLink
from ..security import issue_offline_token, sign_command
from .devices import PtyBridge, SimCabinet, SimPower, SimTransport

log = logging.getLogger(__name__)


class Cloud:
    """What the backend would do: sign commands, issue tokens, watch topics."""

    def __init__(self, cfg, secret: bytes, link, mqtt_addr: str | None):
        self.cfg, self.secret, self.link = cfg, secret, link
        self.base = f"cabinet/{cfg.cabinet.id}/"
        self.received: collections.deque = collections.deque(maxlen=300)
        self.last_command: dict | None = None
        self.client = None
        if mqtt_addr:
            import paho.mqtt.client as mqtt
            host, _, port = mqtt_addr.partition(":")
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="nook-sim-cloud")
            c.on_connect = lambda cl, *a: cl.subscribe(self.base + "#", qos=1)
            c.on_message = self._message
            c.connect(host, int(port or 1883))
            c.loop_start()
            self.client = c

    def _message(self, client, userdata, msg):
        if "/cmd/" in msg.topic:
            return
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            payload = msg.payload.decode(errors="replace")
        self.received.append({"ts": time.time(), "topic": msg.topic, "payload": payload,
                              "retained": bool(msg.retain)})

    def messages(self) -> list[dict]:
        if self.client:
            return list(self.received)
        return list(self.link.sent)

    def status(self) -> dict | None:
        if self.client:
            for m in reversed(self.received):
                if m["topic"].endswith("/status"):
                    return m["payload"]
            return None
        return self.link.retained.get(self.base + "status")

    def send(self, cmd: dict, sub: str = "open") -> bool:
        if self.client:
            if not self.link.connected:
                return False
            self.client.publish(self.base + "cmd/" + sub, json.dumps(cmd), qos=1)
            return True
        return self.link.inject(sub, cmd)

    def app_unlock(self, locker: int, member: str, ttl: int = 30, exp_offset: int = 0,
                   forge: bool = False) -> tuple[bool, dict]:
        cmd = sign_command(self.secret, self.cfg.cabinet.id, locker, str(uuid.uuid4()),
                           int(time.time()) + ttl + exp_offset, member)
        if forge:
            cmd["sig"] = secrets.token_hex(32)
        self.last_command = cmd
        return self.send(cmd), cmd

    def offline_token(self, locker: int, member: str) -> str:
        now = int(time.time())
        return issue_offline_token(self.secret, self.cfg.cabinet.id, locker, member,
                                   now - 60, now + 24 * 3600, uuid.uuid4().hex)


class SimSession:
    def __init__(self, args):
        cfg = config_mod.load(None)
        cfg.cabinet.id = "sim-001"
        cfg.mqtt.heartbeat = 5
        cfg.doors.poll_interval = 0.3
        cfg.doors.left_open_alarm = 30
        cfg.power.low_battery = 30
        cfg.power.shutdown_below = 0
        cfg.storage.db = args.db or os.path.join(tempfile.mkdtemp(prefix="nook-sim-"), "journal.db")
        self.cfg = cfg
        self.secret = secrets.token_bytes(32)
        self.cabinet = SimCabinet(cfg.cabinet.lockers, cfg.cabinet.locks_per_board,
                                  cfg.cabinet.first_board_address, args.auto_close)
        self.power = SimPower(self.cabinet)
        if args.pty:
            from ..bus import open_serial
            bridge = PtyBridge(self.cabinet)
            bridge.start()
            transport = open_serial(bridge.port, timeout=0.2)
            log.info("simulated boards on virtual serial port %s", bridge.port)
        else:
            transport = SimTransport(self.cabinet)
        if args.mqtt:
            host, _, port = args.mqtt.partition(":")
            self.link = MqttLink(cfg.cabinet.id, host, int(port or 1883), tls=False)
        else:
            self.link = LoopbackLink(cfg.cabinet.id)
        self.ctl = build_controller(cfg, transport, self.link, self.power, self.secret)
        self.cloud = Cloud(cfg, self.secret, self.link, args.mqtt)
        self.internet = True
        self.lock = threading.Lock()
        self.notes: collections.deque = collections.deque(maxlen=50)

    def note(self, text: str) -> None:
        self.notes.append({"ts": time.time(), "text": text})

    def set_internet(self, on: bool) -> None:
        self.internet = on
        if isinstance(self.link, LoopbackLink):
            self.link.set_online(on)
        else:
            self.link.set_blocked(not on)
        self.note("Internet restored" if on else "Internet cut")

    def state(self) -> dict:
        return {
            "cabinet": self.cfg.cabinet.id,
            "lockers": self.cabinet.snapshot(),
            "mains": self.power.mains,
            "battery": round(self.power.battery, 1),
            "powered": self.cabinet.powered,
            "internet": self.internet,
            "linkConnected": self.link.connected,
            "mode": "mqtt" if self.cloud.client else "loopback",
            "boards": {str(a): b.fault for a, b in self.cabinet.boards.items()},
            "controller": self.ctl.status(),
            "cloudStatus": self.cloud.status(),
            "cloud": self.cloud.messages()[-60:][::-1],
            "journal": self.ctl.journal.recent(60),
            "notes": list(self.notes)[::-1],
        }

    def action(self, data: dict) -> dict:
        a = data.get("action")
        n = data.get("locker")
        locker = n if isinstance(n, int) and 1 <= n <= self.cfg.cabinet.lockers else None
        member = f"m-{(locker or 0) + 100}"
        if a == "app-unlock" and locker:
            sent, _ = self.cloud.app_unlock(locker, member)
            self.note(f"App: unlock locker {locker}" if sent else
                      f"App: cabinet offline, locker {locker} not opened (use Bluetooth)")
            return {"ok": sent}
        if a == "ble-unlock" and locker:
            res = self.ctl.handle_offline_token(self.cloud.offline_token(locker, member))
            self.note(f"Bluetooth: locker {locker} -> {res.get('reason') or 'opened'}")
            return res
        if a == "close" and locker:
            self.cabinet.set_door(locker, False)
            self.note(f"Door {locker} closed by hand")
            return {"ok": True}
        if a == "force" and locker:
            self.cabinet.set_door(locker, True)
            self.note(f"Door {locker} forced open")
            return {"ok": True}
        if a == "jam" and locker:
            self.cabinet.set_jammed(locker, bool(data.get("on")))
            return {"ok": True}
        if a == "mains":
            self.power.set_mains(bool(data.get("on")))
            self.note("Mains restored" if data.get("on") else "Power cut, running on battery")
            return {"ok": True}
        if a == "internet":
            self.set_internet(bool(data.get("on")))
            return {"ok": True}
        if a == "board-fault":
            addr = data.get("board")
            if isinstance(addr, int) and addr in self.cabinet.boards:
                self.cabinet.boards[addr].fault = bool(data.get("on"))
                self.note(f"Board {addr} {'disconnected' if data.get('on') else 'reconnected'}")
                return {"ok": True}
        if a == "replay" and self.cloud.last_command:
            sent = self.cloud.send(self.cloud.last_command)
            self.note("Security test: replayed the last command")
            return {"ok": sent}
        if a == "expired" and locker:
            self.cloud.app_unlock(locker, member, exp_offset=-120)
            self.note(f"Security test: expired command for locker {locker}")
            return {"ok": True}
        if a == "forged" and locker:
            self.cloud.app_unlock(locker, member, forge=True)
            self.note(f"Security test: forged signature for locker {locker}")
            return {"ok": True}
        return {"ok": False, "reason": "unknown action"}


def run_sim(args) -> int:
    from .dashboard import serve

    s = SimSession(args)
    s.ctl.start()
    host, _, port = args.http.partition(":")
    print(f"\n  Nook simulator running: http://{host}:{port or 8080}\n"
          f"  Cloud link: {'MQTT ' + args.mqtt if args.mqtt else 'in-memory'}"
          f"  ·  journal: {s.cfg.storage.db}\n  Ctrl+C to stop.\n")
    try:
        serve(s, host, int(port or 8080))
    except KeyboardInterrupt:
        pass
    finally:
        s.ctl.stop()
    return 0
