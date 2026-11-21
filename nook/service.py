"""The cabinet controller: ties the lock boards, the cloud link, the
Bluetooth fallback, power monitoring and the local journal together."""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import threading
import time

from . import __version__
from .config import Config
from .journal import Journal
from .link import Link
from .locks import DoorWatcher, LockController
from .power import PowerMonitor
from .security import Verdict, Verifier

log = logging.getLogger(__name__)


class Controller:
    def __init__(self, cfg: Config, locks: LockController, link: Link, journal: Journal,
                 verifier: Verifier, power: PowerMonitor, clock=time.time):
        self.cfg = cfg
        self.locks = locks
        self.link = link
        self.journal = journal
        self.verifier = verifier
        self.power = power
        self.clock = clock
        self.started = time.monotonic()
        self.watcher = DoorWatcher(
            locks, self._door_changed, self._door_left_open, self._boards_changed,
            interval=cfg.doors.poll_interval, left_open_after=cfg.doors.left_open_alarm)
        self.power_state = power.read()
        self._last_unlock: dict[int, float] = {}
        self._unlock_lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._stop = threading.Event()
        self._low_battery_sent = False
        self._shutdown_started = False
        self._loop = threading.Thread(target=self._housekeeping, name="housekeeping", daemon=True)

    # lifecycle --------------------------------------------------------------
    def start(self) -> None:
        log.info("Nook controller %s, cabinet %s, %d lockers on %d board(s)",
                 __version__, self.cfg.cabinet.id, self.locks.lockers, self.locks.boards)
        self.watcher.poll_once()                  # learn current door states
        self.watcher.start()
        self.link.start(self._on_command, self._on_connect)
        self._loop.start()

    def stop(self) -> None:
        self._stop.set()
        self.watcher.stop()
        self.link.stop()

    # events -----------------------------------------------------------------
    def emit(self, topic: str, payload: dict) -> None:
        payload.setdefault("ts", int(self.clock()))
        self.journal.record(topic, payload)
        self.flush()

    def flush(self) -> int:
        """Publish journaled events in order; stop at the first failure."""
        sent = 0
        with self._flush_lock:
            while True:
                batch = self.journal.pending(100)
                if not batch:
                    return sent
                for event_id, topic, payload in batch:
                    if not self.link.publish(topic, payload):
                        return sent
                    self.journal.mark_sent(event_id)
                    sent += 1

    def status(self) -> dict:
        return {
            "state": "online",
            "version": __version__,
            "uptime": int(time.monotonic() - self.started),
            "power": self.power_state.as_dict(),
            "boards": {"total": self.locks.boards, "failed": self.watcher.failed_boards},
            "openDoors": sorted(k for k, v in self.watcher.doors.items() if v),
            "pendingEvents": self.journal.pending_count(),
            "ts": int(self.clock()),
        }

    def publish_status(self) -> None:
        self.link.publish("status", self.status(), retain=True)

    def publish_doors(self) -> None:
        doors, failed = self.locks.read_all()          # fresh read, not the cache
        self.link.publish("doors", {
            "doors": {str(k): v for k, v in sorted(doors.items())},
            "failedBoards": failed, "ts": int(self.clock())})

    def _on_connect(self) -> None:
        n = self.flush()
        if n:
            log.info("sent %d event(s) recorded while offline", n)
        self.publish_status()

    # commands ---------------------------------------------------------------
    def _on_command(self, sub: str, payload: bytes) -> None:
        if sub == "open":
            self.handle_open(payload)
        elif sub == "sync":
            self.publish_doors()
            self.publish_status()
        else:
            log.debug("ignored command %s", sub)

    def handle_open(self, payload: bytes) -> dict:
        try:
            cmd = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            cmd = None
        verdict = self.verifier.command(cmd) if isinstance(cmd, dict) else Verdict(False, "not JSON")
        if not verdict.ok:
            rid = cmd.get("requestId") if isinstance(cmd, dict) else None
            return self._reject(verdict, "app", rid if isinstance(rid, str) else None)
        return self._unlock(verdict, "app")

    def handle_offline_token(self, token: str) -> dict:
        verdict = self.verifier.offline_token(token)
        if not verdict.ok:
            return self._reject(verdict, "ble", None)
        return self._unlock(verdict, "ble")

    def _reject(self, verdict: Verdict, source: str, ref: str | None) -> dict:
        log.warning("%s unlock refused: %s", source, verdict.reason)
        event = {"ok": False, "reason": verdict.reason, "source": source}
        if verdict.locker:
            event["locker"] = verdict.locker
        if ref:
            event["requestId"] = ref[:64]
        self.emit("event/unlock", event)
        return {"ok": False, "reason": verdict.reason}

    def _unlock(self, verdict: Verdict, source: str) -> dict:
        locker = verdict.locker
        with self._unlock_lock:
            now = time.monotonic()
            last = self._last_unlock.get(locker, -1e9)
            if now - last < self.cfg.security.min_unlock_interval:
                return self._reject(Verdict(False, "too many requests", locker), source, verdict.ref)
            self._last_unlock[locker] = now
            result = self.locks.unlock(locker)
        event = {"locker": locker, "ok": result.ok, "doorOpen": result.door_open,
                 "source": source, "requestId" if source == "app" else "nonce": verdict.ref}
        if verdict.member:
            event["member"] = verdict.member
        if result.error:
            event["reason"] = result.error
        log.info("unlock locker %d via %s: %s", locker, source,
                 "opened" if result.door_open else (result.error or "no door movement"))
        self.emit("event/unlock", event)
        return {"ok": result.ok, "locker": locker, "doorOpen": result.door_open,
                **({"reason": result.error} if result.error else {})}

    # door and board callbacks -------------------------------------------------
    def _door_changed(self, locker: int, is_open: bool) -> None:
        event = "opened" if is_open else "closed"
        if is_open and time.monotonic() - self._last_unlock.get(locker, -1e9) > 10:
            event = "forced"                  # opened with no unlock just before
            log.error("locker %d opened without an unlock command", locker)
        self.emit("event/door", {"locker": locker, "event": event})

    def _door_left_open(self, locker: int, _: bool) -> None:
        self.emit("event/door", {"locker": locker, "event": "left_open",
                                 "after": int(self.cfg.doors.left_open_alarm)})

    def _boards_changed(self, failed: list[int]) -> None:
        if failed:
            log.error("lock board(s) not answering: %s", failed)
        self.emit("event/fault", {"failedBoards": failed})

    # power, heartbeat, cleanup ------------------------------------------------
    def check_power(self) -> None:
        before, now = self.power_state, self.power.read()
        self.power_state = now
        if before.mains is not None and now.mains is not None and before.mains != now.mains:
            self.emit("event/power", {"event": "mains_restored" if now.mains else "on_battery",
                                      **now.as_dict()})
            self.publish_status()
        pct = now.battery_pct
        if now.mains is False and pct is not None:
            if pct <= self.cfg.power.low_battery and not self._low_battery_sent:
                self._low_battery_sent = True
                self.emit("event/power", {"event": "low_battery", **now.as_dict()})
            limit = self.cfg.power.shutdown_below
            if limit and pct <= limit and not self._shutdown_started:
                self._shutdown_started = True
                self.emit("event/power", {"event": "shutdown", **now.as_dict()})
                log.critical("battery at %d%%, shutting down", pct)
                subprocess.Popen(shlex.split(self.cfg.power.shutdown_command))
        if now.mains:
            self._low_battery_sent = False

    def _housekeeping(self) -> None:
        last_beat = last_prune = 0.0
        while not self._stop.wait(1.0):
            try:
                self.check_power()
                t = time.monotonic()
                if t - last_beat >= self.cfg.mqtt.heartbeat:
                    last_beat = t
                    self.publish_status()
                    self.flush()
                if t - last_prune >= 86400:
                    last_prune = t
                    self.journal.prune(self.cfg.storage.keep_days)
            except Exception:
                log.exception("housekeeping failed")
