"""Cloud link. MqttLink talks to the real broker; LoopbackLink is the
in-memory stand-in the simulator uses when no broker is running.

Topics (prefix cabinet/{id}/):
    cmd/open        in   signed unlock command (see security.py)
    cmd/sync        in   ask for a full door report
    event/unlock    out  result of every unlock attempt
    event/door      out  a door opened or closed, or was left open
    event/fault     out  a lock board stopped / started answering
    event/power     out  mains lost / restored, low battery
    doors           out  full door report (answer to cmd/sync)
    status          out  retained heartbeat; Last Will = {"state": "offline"}
"""

from __future__ import annotations

import collections
import json
import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

CommandFn = Callable[[str, bytes], None]     # (sub-topic after cmd/, payload)
ConnectFn = Callable[[], None]


def _encode(payload) -> bytes:
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    if isinstance(payload, str):
        return payload.encode()
    return json.dumps(payload, separators=(",", ":")).encode()


class Link:
    connected: bool = False

    def start(self, on_command: CommandFn, on_connect: ConnectFn) -> None: ...
    def publish(self, subtopic: str, payload, retain: bool = False) -> bool: ...
    def stop(self) -> None: ...


class MqttLink(Link):
    def __init__(self, cabinet: str, host: str, port: int = 8883, tls: bool = True,
                 ca: str | None = None, cert: str | None = None, key: str | None = None,
                 username: str | None = None, password: str | None = None,
                 keepalive: int = 30):
        import paho.mqtt.client as mqtt   # only needed when a broker is used

        self._mqtt = mqtt
        self.base = f"cabinet/{cabinet}/"
        self.host, self.port, self.keepalive = host, port, keepalive
        self.connected = False
        self._blocked = False
        self._on_command: CommandFn | None = None
        self._on_connect: ConnectFn | None = None
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"nook-{cabinet}",
                        clean_session=False)
        if tls:
            c.tls_set(ca_certs=ca, certfile=cert, keyfile=key)
        if username:
            c.username_pw_set(username, password)
        c.will_set(self.base + "status", _encode({"state": "offline"}), qos=1, retain=True)
        c.reconnect_delay_set(min_delay=1, max_delay=60)
        c.on_connect = self._connected
        c.on_disconnect = self._disconnected
        c.on_message = self._message
        self.client = c

    def start(self, on_command: CommandFn, on_connect: ConnectFn) -> None:
        self._on_command, self._on_connect = on_command, on_connect
        self.client.connect_async(self.host, self.port, keepalive=self.keepalive)
        self.client.loop_start()

    def _connected(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            log.error("broker refused connection: %s", reason_code)
            return
        log.info("connected to %s:%s", self.host, self.port)
        self.connected = True
        client.subscribe(self.base + "cmd/#", qos=1)
        if self._on_connect:
            threading.Thread(target=self._on_connect, name="on-connect", daemon=True).start()

    def _disconnected(self, client, userdata, flags, reason_code, properties=None):
        if self.connected:
            log.warning("disconnected from broker: %s", reason_code)
        self.connected = False

    def _message(self, client, userdata, msg):
        sub = msg.topic[len(self.base + "cmd/"):]
        if self._on_command:
            try:
                self._on_command(sub, msg.payload)
            except Exception:
                log.exception("command handler failed")

    def publish(self, subtopic: str, payload, retain: bool = False) -> bool:
        if not self.connected or self._blocked:
            return False
        info = self.client.publish(self.base + subtopic, _encode(payload), qos=1, retain=retain)
        return info.rc == self._mqtt.MQTT_ERR_SUCCESS

    def set_blocked(self, blocked: bool) -> None:
        """Simulator only: pretend the internet is cut / restored."""
        if blocked and not self._blocked:
            self._blocked = True
            self.client.publish(self.base + "status", _encode({"state": "offline"}),
                                qos=1, retain=True)
            time.sleep(0.2)
            self.client.disconnect()
        elif not blocked and self._blocked:
            self._blocked = False
            self.client.loop_stop()           # the network thread ends after disconnect()
            self.client.connect_async(self.host, self.port, keepalive=self.keepalive)
            self.client.loop_start()

    def stop(self) -> None:
        if self.connected:
            self.client.publish(self.base + "status", _encode({"state": "offline"}),
                                qos=1, retain=True).wait_for_publish(2)
        self.client.disconnect()
        self.client.loop_stop()


class LoopbackLink(Link):
    """In-memory cloud for the simulator and tests."""

    def __init__(self, cabinet: str):
        self.base = f"cabinet/{cabinet}/"
        self.connected = False
        self.sent: collections.deque = collections.deque(maxlen=500)
        self.retained: dict[str, dict] = {}
        self._on_command: CommandFn | None = None
        self._on_connect: ConnectFn | None = None
        self._online = True

    def start(self, on_command: CommandFn, on_connect: ConnectFn) -> None:
        self._on_command, self._on_connect = on_command, on_connect
        self.set_online(self._online)

    def set_online(self, online: bool) -> None:
        self._online = online
        was = self.connected
        self.connected = online
        if not online and was:
            self.retained[self.base + "status"] = {"state": "offline"}   # Last Will
        if online and not was and self._on_connect:
            self._on_connect()

    def inject(self, subtopic: str, payload) -> bool:
        """What the cloud would publish on cmd/<subtopic>. False if offline."""
        if not self.connected or not self._on_command:
            return False
        self._on_command(subtopic, _encode(payload))
        return True

    def publish(self, subtopic: str, payload, retain: bool = False) -> bool:
        if not self.connected:
            return False
        topic = self.base + subtopic
        data = json.loads(_encode(payload))
        self.sent.append({"ts": time.time(), "topic": topic, "payload": data})
        if retain:
            self.retained[topic] = data
        return True

    def stop(self) -> None:
        self.connected = False
