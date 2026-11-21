"""Bluetooth LE fallback: members open their locker with an offline token
when the cabinet has no internet.

GATT service (UUIDs are Nook's own, fixed so the app can find the cabinet):
    service   6e6f6f6b-0001-4c6f-636b-657200000000
    unlock    6e6f6f6b-0002-4c6f-636b-657200000000  write   token (UTF-8)
    result    6e6f6f6b-0003-4c6f-636b-657200000000  read/notify  JSON result

Uses the `bless` library (BlueZ over D-Bus on Raspberry Pi OS). The token is
checked by the same Verifier as online commands; nothing else is exposed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)

SERVICE_UUID = "6e6f6f6b-0001-4c6f-636b-657200000000"
UNLOCK_UUID = "6e6f6f6b-0002-4c6f-636b-657200000000"
RESULT_UUID = "6e6f6f6b-0003-4c6f-636b-657200000000"

TokenHandler = Callable[[str], dict]


class BleServer(threading.Thread):
    def __init__(self, name: str, handler: TokenHandler):
        super().__init__(name="ble", daemon=True)
        self.adv_name = name[:20]
        self.handler = handler
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None

    def run(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception:
            log.exception("Bluetooth server stopped")

    async def _serve(self) -> None:
        from bless import (BlessServer, GATTAttributePermissions,
                           GATTCharacteristicProperties)

        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        server = BlessServer(name=self.adv_name, loop=self._loop)
        server.read_request_func = self._on_read
        server.write_request_func = lambda ch, value, **kw: self._on_write(server, ch, value)
        await server.add_new_service(SERVICE_UUID)
        await server.add_new_characteristic(
            SERVICE_UUID, UNLOCK_UUID, GATTCharacteristicProperties.write,
            None, GATTAttributePermissions.writeable)
        await server.add_new_characteristic(
            SERVICE_UUID, RESULT_UUID,
            GATTCharacteristicProperties.read | GATTCharacteristicProperties.notify,
            b"{}", GATTAttributePermissions.readable)
        await server.start()
        log.info("Bluetooth advertising as %s", self.adv_name)
        await self._stop.wait()
        await server.stop()

    def _on_read(self, characteristic, **kwargs):
        return characteristic.value

    def _on_write(self, server, characteristic, value: bytearray) -> None:
        if str(characteristic.uuid).lower() != UNLOCK_UUID:
            return
        try:
            token = bytes(value).decode("utf-8", "strict")
        except UnicodeDecodeError:
            result = {"ok": False, "reason": "bad token"}
        else:
            result = self.handler(token)
        out = server.get_characteristic(RESULT_UUID)
        out.value = json.dumps(result, separators=(",", ":")).encode()
        server.update_value(SERVICE_UUID, RESULT_UUID)

    def stop(self) -> None:
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
