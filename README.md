# Nook Locker controller

The Raspberry Pi program that runs a Nook Locker 100 cabinet, and a simulator to test it without any hardware.

- Drives the Kerong KR-CU48 lock boards over RS485 and polls every door.
- Receives signed unlock commands from the cloud over MQTT/TLS, and reports results and door events.
- Opens lockers over Bluetooth with offline tokens when the internet is down.
- Watches mains power and the UPS battery, and shuts down cleanly when the battery is almost empty.
- Records every event in a local SQLite journal and sends anything recorded offline when the link returns.

## Try it without hardware

You need Python 3.11 or newer. The simulator needs no extra packages and runs on Windows, macOS and Linux.

```bash
python -m nook sim
```

Open http://127.0.0.1:8080. The dashboard shows the 100 compartments, what the cloud receives, and the cabinet's local journal.

Things to try:

| Action | What should happen |
|---|---|
| Click a closed locker (App mode) | The door opens. `event/unlock` with `ok:true`, then `event/door opened` |
| Click an open locker | The door closes. `event/door closed` |
| Cut the Internet, then unlock with the App | Refused: the cabinet is offline |
| Switch to Bluetooth and unlock | The door opens. Events are queued in the journal |
| Restore the Internet | The queued events are sent in order, then a fresh `status` |
| Cut mains power | `event/power on_battery`. The battery drains, and `low_battery` is sent at 30% |
| Unplug board 3 | Lockers 97–100 show "no signal", and `event/fault` is sent |
| Jam a lock, then unlock it | `ok:true, doorOpen:false`: the board answered but the door did not move |
| Force a door open | `event/door forced`: the door opened without an unlock command |
| Run the security tests | Replay, expired and forged commands are all refused and reported |

Options:

```bash
python -m nook sim --auto-close 8          # doors close by themselves after 8 s
python -m nook sim --mqtt localhost:1883   # use a real broker (pip install paho-mqtt)
python -m nook sim --pty                   # Linux/macOS: boards behind a virtual serial port
```

With `--mqtt`, start a broker first, for example `mosquitto -p 1883` or `docker run -p 1883:1883 eclipse-mosquitto:2 mosquitto -c /mosquitto-no-auth.conf`.

The simulator runs the same controller code as the Pi. Only the boards, the power supply and the Bluetooth radio are simulated. The boards answer the same RS485 frames the real ones do.

## Tests

```bash
pip install pytest pyserial
python -m pytest
```

There are 37 tests. They cover:
- frames and checksums;
- signature, expiry, replay and token rules;
- unlocking end to end;
- offline queueing and flushing;
- board faults, jammed locks and forced doors;
- power events and rate limiting;
- the real pyserial path over a virtual serial port.

## Install on the Raspberry Pi

1. Flash Raspberry Pi OS Lite (64-bit, Bookworm). Enable SSH, I2C and Bluetooth.
2. Copy this folder to the Pi, then run:

   ```bash
   sudo ./deploy/install.sh
   ```

   This creates the `nook` user, a virtualenv in `/opt/nook`, `/etc/nook/config.toml`, a random cabinet secret and the systemd service.
3. Edit `/etc/nook/config.toml`. Set the cabinet id, the broker, and the serial port (prefer `/dev/serial/by-id/...`). Set the power pins if a UPS HAT is fitted.
4. Copy the broker certificates to `/etc/nook/`: `ca.pem`, `cabinet.crt` and `cabinet.key`.
5. Register `/etc/nook/secret` for this cabinet in the cloud backend.
6. Check the boards before starting the service:

   ```bash
   sudo -u nook /opt/nook/venv/bin/python -m nook doors  -c /etc/nook/config.toml
   sudo -u nook /opt/nook/venv/bin/python -m nook unlock 1 -c /etc/nook/config.toml
   ```

7. Start the service:

   ```bash
   sudo systemctl start nook-controller
   journalctl -u nook-controller -f
   ```

**Before the first real test, check the board protocol.** The frame layouts in `nook/protocol.py` follow the usual Kerong format:
- unlock: `8A addr ch 11 BCC`
- read all doors: `80 addr 00 33 BCC`

The reply layouts differ between firmware versions. Compare them with the datasheet that comes with your boards. Everything that depends on them is in that one file.

## MQTT topics

The prefix is `cabinet/{id}/`.

| Topic | Direction | Payload |
|---|---|---|
| `cmd/open` | cloud → cabinet | `{"locker":17,"requestId":"uuid","exp":1767225600,"member":"m-42","sig":"hex"}` |
| `cmd/sync` | cloud → cabinet | anything; the cabinet answers on `doors` and `status` |
| `event/unlock` | cabinet → cloud | `{"locker","ok","doorOpen","source":"app" or "ble","requestId" or "nonce","member","reason","ts"}` |
| `event/door` | cabinet → cloud | `{"locker","event":"opened"/"closed"/"forced"/"left_open","ts"}` |
| `event/fault` | cabinet → cloud | `{"failedBoards":[3],"ts"}` (an empty list means everything is back) |
| `event/power` | cabinet → cloud | `{"event":"on_battery"/"mains_restored"/"low_battery"/"shutdown","mains","battery","ts"}` |
| `doors` | cabinet → cloud | `{"doors":{"1":false,…},"failedBoards":[],"ts"}` |
| `status` | cabinet → cloud, retained | heartbeat. The Last Will is `{"state":"offline"}` |

Every event carries `ts`, the time it happened on the cabinet. Events sent after an outage therefore keep their real time.

## Security

- **Online commands.** The signature is `HMAC-SHA256(secret, "{cabinet}|{locker}|{requestId}|{exp}")`. The cabinet id is part of what is signed, so a command for one cabinet cannot open another. A command is refused if it:
  - has expired;
  - expires more than 60 s ahead;
  - has already been used (its `requestId` is kept in the journal until it expires).
- **Offline tokens.** The format is `v1.<payload>.<signature>`. The token is signed with a key derived from the cabinet secret, so an online signature can never pass as a token. Each token is for one cabinet and one locker, has a validity window of 7 days at most, and can be used once.
- **Transport.** The broker connection uses TLS with a client certificate per cabinet.
- **Logging.** Every attempt is recorded and reported, including refused ones. A door that opens without a command raises `forced`.
- **Rate limit.** Each locker can be unlocked at most once every 2 s.

The cloud-side signing code is in `nook/security.py` (`sign_command`, `issue_offline_token`), so the backend can reuse the same definitions. To test against a real cabinet:

```bash
python -m nook sign-command 17 --member m-42 -c /etc/nook/config.toml
python -m nook issue-token 17 --member m-42 --hours 24 -c /etc/nook/config.toml
```

## Bluetooth fallback

The cabinet advertises as `Nook-<cabinet id>` with one GATT service:

| Characteristic | UUID | Use |
|---|---|---|
| service | `6e6f6f6b-0001-4c6f-636b-657200000000` | |
| unlock | `…0002…` | write the offline token (UTF-8) |
| result | `…0003…` | read / notify `{"ok":true,"locker":17,"doorOpen":true}` |

The member's app downloads a few tokens while it is online. When the cabinet is offline, the app writes a token to the unlock characteristic.

## Layout

```
nook/
  protocol.py   Kerong frames and checksums (check against your datasheet)
  bus.py        RS485 transport, one request at a time, retries
  locks.py      locker → board/channel, unlock, door polling
  security.py   signed commands, offline tokens
  journal.py    SQLite audit log, outbox, used ids
  link.py       MQTT link (paho) and the in-memory link for the simulator
  power.py      mains GPIO and INA219 battery monitor
  ble.py        Bluetooth GATT server (bless)
  service.py    the controller itself
  sim/          simulated boards, power and dashboard
deploy/         install script and systemd unit
tests/          pytest suite
```
