"""Mains and battery monitoring.

Mains detection: a GPIO input that is HIGH while the 12 V supply is present
(e.g. through an optocoupler or a voltage divider from the PSU output).
Battery level: an INA219 on I2C, as on common Raspberry Pi UPS HATs
(Waveshare UPS HAT (B) uses address 0x42 with a 2S pack, 6.0-8.4 V).
Both are optional; without them the cabinet reports "unknown".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class PowerState:
    mains: bool | None          # None = not monitored
    battery_pct: float | None   # None = not monitored

    def as_dict(self) -> dict:
        return {"mains": self.mains,
                "battery": None if self.battery_pct is None else round(self.battery_pct)}


class PowerMonitor:
    def read(self) -> PowerState:
        return PowerState(None, None)

    def close(self) -> None:
        pass


class PiPowerMonitor(PowerMonitor):
    def __init__(self, mains_gpio: int = 0, ina219_address: int = 0,
                 i2c_bus: int = 1, empty_v: float = 6.0, full_v: float = 8.4):
        self._mains = None
        self._i2c = None
        self.addr = ina219_address
        self.empty_v, self.full_v = empty_v, full_v
        if mains_gpio:
            try:
                from gpiozero import DigitalInputDevice
                self._mains = DigitalInputDevice(mains_gpio, pull_up=False)
            except Exception as exc:
                log.warning("mains GPIO %s unavailable: %s", mains_gpio, exc)
        if ina219_address:
            try:
                from smbus2 import SMBus
                self._i2c = SMBus(i2c_bus)
            except Exception as exc:
                log.warning("I2C bus %s unavailable: %s", i2c_bus, exc)

    def _battery_volts(self) -> float | None:
        if not self._i2c:
            return None
        try:
            raw = self._i2c.read_word_data(self.addr, 0x02)       # bus voltage register
            raw = ((raw & 0xFF) << 8) | (raw >> 8)                # INA219 is big-endian
            return (raw >> 3) * 0.004
        except OSError as exc:
            log.debug("INA219 read failed: %s", exc)
            return None

    def read(self) -> PowerState:
        mains = bool(self._mains.value) if self._mains else None
        volts = self._battery_volts()
        pct = None
        if volts is not None:
            pct = max(0.0, min(100.0, (volts - self.empty_v) / (self.full_v - self.empty_v) * 100))
        return PowerState(mains, pct)

    def close(self) -> None:
        if self._mains:
            self._mains.close()
        if self._i2c:
            self._i2c.close()
