"""Configuration, read from a TOML file (see config.example.toml)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields


@dataclass
class Cabinet:
    id: str = "cab-001"
    lockers: int = 100
    locks_per_board: int = 48
    first_board_address: int = 1


@dataclass
class Serial:
    port: str = "/dev/ttyUSB0"
    baudrate: int = 9600
    timeout: float = 0.3
    retries: int = 2


@dataclass
class Mqtt:
    enabled: bool = True
    host: str = "mqtt.example.com"
    port: int = 8883
    tls: bool = True
    ca: str = "/etc/nook/ca.pem"
    cert: str = "/etc/nook/cabinet.crt"
    key: str = "/etc/nook/cabinet.key"
    username: str = ""
    password: str = ""
    heartbeat: int = 30


@dataclass
class Security:
    secret_file: str = "/etc/nook/secret"
    max_command_ttl: int = 60
    clock_skew: int = 5
    max_offline_days: int = 7
    min_unlock_interval: float = 2.0


@dataclass
class Doors:
    poll_interval: float = 0.5
    left_open_alarm: float = 120.0


@dataclass
class Power:
    mains_gpio: int = 0
    ina219_address: int = 0
    i2c_bus: int = 1
    battery_empty_v: float = 6.0
    battery_full_v: float = 8.4
    low_battery: int = 20
    shutdown_below: int = 0
    shutdown_command: str = "sudo systemctl poweroff"


@dataclass
class Ble:
    enabled: bool = True
    name: str = ""


@dataclass
class Storage:
    db: str = "/var/lib/nook/journal.db"
    keep_days: int = 90


@dataclass
class Config:
    cabinet: Cabinet = field(default_factory=Cabinet)
    serial: Serial = field(default_factory=Serial)
    mqtt: Mqtt = field(default_factory=Mqtt)
    security: Security = field(default_factory=Security)
    doors: Doors = field(default_factory=Doors)
    power: Power = field(default_factory=Power)
    ble: Ble = field(default_factory=Ble)
    storage: Storage = field(default_factory=Storage)

    def secret(self) -> bytes:
        env = os.environ.get("NOOK_SECRET")
        if env:
            return env.encode()
        with open(self.security.secret_file, "rb") as f:
            return f.read().strip()


def _fill(section, values: dict, name: str) -> None:
    known = {f.name: f for f in fields(section)}
    for key, value in values.items():
        if key not in known:
            raise ValueError(f"unknown setting [{name}] {key}")
        current = getattr(section, key)
        if isinstance(current, float) and isinstance(value, int):
            value = float(value)
        if type(value) is not type(current):
            raise ValueError(f"[{name}] {key} should be {type(current).__name__}")
        setattr(section, key, value)


def load(path: str | None) -> Config:
    cfg = Config()
    if path:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        for name, values in data.items():
            if not hasattr(cfg, name) or not isinstance(values, dict):
                raise ValueError(f"unknown section [{name}]")
            _fill(getattr(cfg, name), values, name)
    c = cfg.cabinet
    if not 1 <= c.lockers <= 48 * 32:
        raise ValueError("cabinet.lockers out of range")
    if not cfg.ble.name:
        cfg.ble.name = f"Nook-{c.id}"
    return cfg
