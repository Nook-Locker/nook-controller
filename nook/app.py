"""Assembling a controller from a config (shared by the CLI and the simulator)."""

from __future__ import annotations

from .bus import Bus
from .journal import Journal
from .locks import LockController
from .security import Verifier
from .service import Controller


def build_locks(cfg, transport) -> LockController:
    return LockController(Bus(transport, retries=cfg.serial.retries), cfg.cabinet.lockers,
                          cfg.cabinet.locks_per_board, cfg.cabinet.first_board_address)


def build_controller(cfg, transport, link, power, secret: bytes) -> Controller:
    journal = Journal(cfg.storage.db)
    verifier = Verifier(secret, cfg.cabinet.id, cfg.cabinet.lockers, journal.remember,
                        cfg.security.max_command_ttl, cfg.security.clock_skew,
                        cfg.security.max_offline_days)
    return Controller(cfg, build_locks(cfg, transport), link, journal, verifier, power)
