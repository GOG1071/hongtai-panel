"""Wire protocol for HONGTAI / LOVINGCOOL USB serial LCD panels (33c3:7791).

Reverse-engineered from the Windows Electron app "LOVINGCOOL MONITOR"
(main/_baseClass/device.js). The panel enumerates as USB CDC-ACM, so the
in-tree cdc_acm driver already exposes it at /dev/ttyACM*; there is no
kernel-level driver to port.

Two distinct wire formats share the port:

  Control frames (commands + JSON replies)
      55 AA | len_lo len_hi | key | payload | ck_lo ck_hi
      len = len(payload) + 7
      ck  = sum(every preceding byte) & 0xFFFF

  Image frames (firmware > 2.8)
      len_le32 | jpeg | ck_lo ck_hi
      ck = sum(len_le32 bytes + jpeg bytes) & 0xFFFF

  Firmware <= 2.8 takes a bare JPEG with no header or checksum.
  SPI-panel models take a bare big-endian RGB565 buffer instead of JPEG.
"""

from __future__ import annotations

import fcntl
import json
import logging
import termios
import threading
import time
from dataclasses import dataclass, field

import serial

log = logging.getLogger(__name__)

MAGIC = b"\x55\xaa"

# Sent raw to abort a partially-transmitted frame and blank the panel.
CLEAR_SEQUENCE = b"\xff\xd9\xff\xd9"
CLEAR_TAIL = b"\x00\x00\x00\x00"

# The firmware drops out of live mode if it stops hearing opcode 0x11.
KEEPALIVE_INTERVAL = 1.5

# The vendor app writes file uploads in 20 KiB chunks, draining between each.
CHUNK_SIZE = 20 * 1024

BAUD = 2_000_000  # Ignored by CDC-ACM, but matches the vendor app.

USB_VID = 0x33C3
USB_PID = 0x7791


class Opcode:
    RESTART = 0x01
    SET_BRIGHTNESS = 0x03
    GET_DEVICE_INFO = 0x06
    OTA_BEGIN = 0x0C
    KEEPALIVE = 0x11
    SET_MOTION_BEFORE_OFF = 0x14
    SET_MOTION_TIMEOUT = 0x15
    SET_REGION = 0x20
    CLOSE = 0x21
    SET_SERIAL_NUMBER = 0x23
    SET_MOTOR = 0x25
    SET_REALTIME_TIMEOUT = 0x26


# Non-JSON replies are one of these single-byte status codes, hex-encoded.
ERROR_CODES = {
    "01": "operation failed",
    "02": "out of memory",
    "03": "internal storage full",
    "04": "SD card full",
    "05": "file does not exist",
    "06": "file open failed",
    "07": "file write failed",
}


class PanelError(RuntimeError):
    """Device reported an error, or the link misbehaved."""


def checksum(data: bytes) -> bytes:
    return (sum(data) & 0xFFFF).to_bytes(2, "little")


def control_frame(key: int, payload: bytes = b"") -> bytes:
    """Build a 55AA control frame."""
    body = MAGIC + (len(payload) + 7).to_bytes(2, "little") + bytes([key]) + payload
    return body + checksum(body)


def image_frame(jpeg: bytes) -> bytes:
    """Build a length-prefixed image frame (firmware > 2.8)."""
    body = len(jpeg).to_bytes(4, "little") + jpeg
    return body + checksum(body)


def parse_reply(raw: bytes) -> dict:
    """Decode a control reply. Payload sits between the 5-byte head and 2-byte checksum."""
    if len(raw) < 8:
        raise PanelError(f"short reply ({len(raw)} bytes): {raw.hex()}")
    payload = raw[5:-2]
    try:
        return {"status": 200, **json.loads(payload.decode("utf8"))}
    except (UnicodeDecodeError, json.JSONDecodeError):
        code = payload.hex()
        raise PanelError(f"device error {code}: {ERROR_CODES.get(code, 'unknown')}") from None


def to_rgb565(rgb: bytes, count: int) -> bytes:
    """Pack RGB888 bytes into big-endian RGB565, for SPI-class panels."""
    out = bytearray(count * 2)
    for i in range(count):
        r, g, b = rgb[i * 3], rgb[i * 3 + 1], rgb[i * 3 + 2]
        v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        out[i * 2] = v >> 8
        out[i * 2 + 1] = v & 0xFF
    return bytes(out)


@dataclass
class PanelInfo:
    """Geometry and capabilities reported by opcode 0x06."""

    width: int = 480
    height: int = 480
    angle: int = 0
    version: str = ""
    uid: str = ""
    model: str = ""
    region: str = ""
    brightness: int = 100
    max_kb_override: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def version_number(self) -> float:
        try:
            return float(str(self.version).replace("Ver", "").strip())
        except ValueError:
            return 0.0

    @property
    def uses_length_header(self) -> bool:
        """Firmware after 2.8 expects the len+checksum image envelope."""
        return self.version_number > 2.8

    @property
    def is_spi(self) -> bool:
        """SPI panels take raw RGB565 instead of JPEG.

        Matches the vendor's checkIsSPI exactly. Note this keys off specific
        model strings, not the controller name: TXW818-ST7701S-4.0inch is a
        JPEG panel despite the ST7701S controller.
        """
        m = self.model.lower()
        return "2.99" in m or "txw813-st7789-2.8inch" in m or "qspi" in m

    @property
    def max_frame_kb(self) -> int:
        """Per-model JPEG budget; quality is stepped down until the frame fits."""
        if self.max_kb_override:
            return self.max_kb_override
        model = self.model
        longest = max(self.width, self.height)
        if "10.26" in model:
            return 350
        if "6.67" in model or longest >= 1024:
            return 260
        if "9.16" in model:
            return 120 if self.uses_length_header else 90
        return 80 if self.uses_length_header else 50

    @property
    def frame_rate(self) -> int:
        model = self.model
        longest = max(self.width, self.height)
        if "10.26" in model or "6.67" in model or longest >= 1024:
            return 60
        if "9.16" in model and self.uses_length_header:
            return 60
        return 30


class Panel:
    """A connected LCD panel.

    Writes are serialized behind a lock because the keepalive thread shares the
    port with the frame writer; interleaving the two corrupts both streams.
    """

    def __init__(self, port: str, timeout: float = 2.0):
        self.port_path = port
        self._serial = serial.Serial(port, BAUD, timeout=timeout, write_timeout=5.0)
        # Refuse every later open() of this tty with EBUSY. Tools that probe
        # /dev/ttyACM* (LED controllers, ModemManager) otherwise inject bytes
        # mid-frame and the panel blanks. pyserial's exclusive=True is only an
        # advisory flock, which those tools never check.
        fcntl.ioctl(self._serial.fileno(), termios.TIOCEXCL)
        self._lock = threading.Lock()
        self._keepalive: threading.Thread | None = None
        self._stop = threading.Event()
        self.info = PanelInfo()

    # -- lifecycle ------------------------------------------------------

    def __enter__(self) -> "Panel":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.stop_live()
        try:
            self.clear()
        except Exception:
            pass
        try:
            self._serial.close()
        except Exception:
            pass

    # -- primitives -----------------------------------------------------

    def _write(self, data: bytes) -> None:
        with self._lock:
            self._serial.write(data)
            self._serial.flush()

    def command(self, key: int, payload: bytes = b"", expect_reply: bool = False) -> dict | None:
        """Send a control frame, optionally waiting for the JSON reply."""
        with self._lock:
            if expect_reply:
                self._serial.reset_input_buffer()
            self._serial.write(control_frame(key, payload))
            self._serial.flush()
            if not expect_reply:
                return None
            deadline = time.monotonic() + 3.0
            buf = b""
            while time.monotonic() < deadline:
                chunk = self._serial.read(4096)
                if chunk:
                    buf += chunk
                    if buf.rstrip(b"\x00"):
                        time.sleep(0.05)
                        buf += self._serial.read(4096)
                        break
                else:
                    time.sleep(0.02)
            if not buf:
                raise PanelError(f"no reply to opcode 0x{key:02x}")
            return parse_reply(buf)

    def clear(self) -> None:
        """Abort any in-flight frame and blank the panel."""
        self._write(CLEAR_SEQUENCE)
        self._write(CLEAR_TAIL)

    def connect(self, max_kb_override: int = 0) -> PanelInfo:
        """Reset the link and read device geometry."""
        self.clear()
        time.sleep(0.3)
        reply = self.command(Opcode.GET_DEVICE_INFO, expect_reply=True) or {}
        # Firmware nests the payload: {"status":200,"cmd":"info","data":{...}}
        data = reply.get("data") or reply
        self.info = PanelInfo(
            width=int(data.get("width", 480)),
            height=int(data.get("height", 480)),
            angle=int(data.get("angle", 0) or 0),
            version=str(data.get("version", "")),
            uid=str(data.get("uid", "")),
            model=str(data.get("model", "")),
            region=str(data.get("region", "")),
            brightness=int(data.get("brightness", 100)),
            max_kb_override=max_kb_override,
            raw=data,
        )
        # 90/270 degree panels report pre-rotation geometry.
        if self.info.angle in (90, 270):
            self.info.width, self.info.height = self.info.height, self.info.width
        return self.info

    # -- commands -------------------------------------------------------

    def set_brightness(self, level: int) -> None:
        level = max(0, min(100, int(level)))
        self.command(Opcode.SET_BRIGHTNESS, bytes([level]))

    def keepalive(self) -> None:
        self.command(Opcode.KEEPALIVE)

    def restart(self) -> None:
        self.command(Opcode.RESTART)

    def set_motor(self, on: bool) -> None:
        self._write(CLEAR_SEQUENCE)
        self.command(Opcode.SET_MOTOR, bytes([1 if on else 0]))
        self.command(Opcode.KEEPALIVE)

    # -- frames ---------------------------------------------------------

    def send_frame(self, payload: bytes) -> None:
        """Push one encoded frame (JPEG, or RGB565 on SPI panels)."""
        if self.info.is_spi or not self.info.uses_length_header:
            self._write(payload)
        else:
            self._write(image_frame(payload))

    def send_file(self, data: bytes) -> None:
        """Upload a blob in 20 KiB chunks, matching the vendor app's pacing."""
        for i in range(0, len(data), CHUNK_SIZE):
            self._write(data[i : i + CHUNK_SIZE])

    # -- live mode ------------------------------------------------------

    def start_live(self) -> None:
        """Enter live mode and hold it open with a background keepalive."""
        if self._keepalive and self._keepalive.is_alive():
            return
        self._stop.clear()
        self.command(Opcode.KEEPALIVE)

        def loop() -> None:
            while not self._stop.wait(KEEPALIVE_INTERVAL):
                try:
                    self.command(Opcode.KEEPALIVE)
                except Exception as exc:
                    log.warning("keepalive failed: %s", exc)
                    return

        self._keepalive = threading.Thread(target=loop, daemon=True, name="panel-keepalive")
        self._keepalive.start()

    def stop_live(self) -> None:
        self._stop.set()
        if self._keepalive:
            self._keepalive.join(timeout=2.0)
            self._keepalive = None


def find_panel() -> str | None:
    """Locate the panel's tty by USB VID:PID, falling back to by-id."""
    from serial.tools import list_ports

    for p in list_ports.comports():
        if p.vid == USB_VID and p.pid == USB_PID:
            return p.device
    for p in list_ports.comports():
        if "MONITOR" in (p.product or "").upper() or "HONGTAI" in (p.manufacturer or "").upper():
            return p.device
    return None
