"""Sending ZPL to a printer: network (port 9100), USB device file, CUPS raw queue, or a file."""

from __future__ import annotations

import glob
import os
import re
import shutil
import socket
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import List, Tuple

METHODS = ("network", "device", "cups", "file")
METHOD_LABELS = {
    "network": "Network (TCP 9100)",
    "device": "USB device file",
    "cups": "CUPS printer (raw)",
    "file": "Save to .zpl file",
}


@dataclass
class PrinterConfig:
    name: str = ""
    method: str = "network"
    host: str = "127.0.0.1"   # the bundled fake printer listens here by default
    port: int = 9100
    device: str = "/dev/usb/lp0"
    cups_queue: str = ""
    file_path: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PrinterConfig":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def describe(self) -> str:
        if self.method == "network":
            return f"{self.host}:{self.port}"
        if self.method == "device":
            return self.device
        if self.method == "cups":
            return f"CUPS: {self.cups_queue or '(none)'}"
        return self.file_path or "file"

    def title(self) -> str:
        return self.name or METHOD_LABELS.get(self.method, self.method)


DEFAULT_NAME = "Test printer (this computer)"


def load_printers(settings: dict) -> Tuple[List[PrinterConfig], int]:
    """Saved printers and the index of the default one.

    Older settings kept one unnamed printer under "printer"; it becomes the
    only (and default) entry. With nothing saved, the list holds the local
    fake printer so Print works out of the box."""
    raw = settings.get("printers")
    if isinstance(raw, list) and raw:
        printers = [PrinterConfig.from_dict(d) for d in raw if isinstance(d, dict)]
    elif isinstance(settings.get("printer"), dict):
        printers = [PrinterConfig.from_dict(settings["printer"])]
    else:
        printers = []
    if not printers:
        printers = [PrinterConfig(name=DEFAULT_NAME)]
    unique_names(printers)
    default = settings.get("default_printer")
    index = next((i for i, p in enumerate(printers) if p.name == default), 0)
    return printers, index


def store_printers(settings: dict, printers: List[PrinterConfig], default: int) -> None:
    unique_names(printers)
    settings["printers"] = [p.to_dict() for p in printers]
    settings["default_printer"] = printers[default].name if printers else ""
    settings.pop("printer", None)


def unique_names(printers: List[PrinterConfig]) -> None:
    """Give every printer a non-empty name that no other printer uses."""
    seen = set()
    for i, p in enumerate(printers, 1):
        base = p.name.strip() or f"Printer {i}"
        name, n = base, 2
        while name in seen:
            name, n = f"{base} ({n})", n + 1
        p.name = name
        seen.add(name)


class PrintError(Exception):
    pass


def usb_devices() -> List[str]:
    return sorted(glob.glob("/dev/usb/lp*"))


def cups_queues() -> List[str]:
    if not shutil.which("lpstat"):
        return []
    try:
        out = subprocess.run(["lpstat", "-e"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def test(cfg: PrinterConfig, timeout: float = 3.0) -> str:
    """Check the printer can be reached without printing anything. Raises PrintError."""
    if cfg.method == "network":
        try:
            with socket.create_connection((cfg.host, int(cfg.port)), timeout=timeout):
                pass
        except OSError as e:
            raise PrintError(f"Nothing answered at {cfg.host}:{cfg.port} ({e.strerror or e}).")
        return f"Printer at {cfg.host}:{cfg.port} is reachable."
    if cfg.method == "device":
        if not os.path.exists(cfg.device):
            raise PrintError(f"{cfg.device} does not exist. Is the printer plugged in and on?")
        if not os.access(cfg.device, os.W_OK):
            raise PrintError(f"No permission to write {cfg.device}. Add yourself to the 'lp' "
                             "group and log in again.")
        return f"{cfg.device} is ready."
    if cfg.method == "cups":
        if not cfg.cups_queue:
            raise PrintError("Choose a CUPS printer first.")
        if cfg.cups_queue not in cups_queues():
            raise PrintError(f"CUPS has no printer called '{cfg.cups_queue}'.")
        return f"CUPS printer '{cfg.cups_queue}' exists."
    return "Print jobs will be saved as .zpl files."


# ---- asking the printer ------------------------------------------------------

# Widest print in mm, by Zebra model (the start of the name ~HI reports).
MAX_WIDTH_MM = {
    "GK420": 104, "GX420": 104, "GX430": 104, "GC420": 104, "GT800": 104, "GT810": 104,
    "GT820": 104, "GT830": 104, "LP2844": 104, "TLP2844": 104, "LP2824": 56,
    "TLP2824": 56, "ZD220": 104, "ZD230": 104, "ZD410": 56, "ZD411": 56, "ZD420": 104,
    "ZD421": 104, "ZD500": 104, "ZD510": 51, "ZD620": 104, "ZD621": 104, "ZD888": 104,
    "ZT230": 104, "ZT231": 104, "ZT410": 104, "ZT411": 104, "ZT420": 168, "ZT421": 168,
    "ZT510": 104, "ZT610": 104, "ZT620": 168, "ZM400": 104, "ZM600": 168, "105SL": 104,
    "110XI4": 104, "140XI4": 128, "170XI4": 168, "220XI4": 216, "S4M": 104,
}
DPMM_TO_DPI = {6: 152, 8: 203, 12: 300, 24: 600}


@dataclass
class PrinterInfo:
    model: str                   # e.g. "GX420d"
    firmware: str
    dpi: int                     # 0 when the printer didn't say
    max_width_dots: int = 0      # 0 when the model isn't in MAX_WIDTH_IN

    def describe(self) -> str:
        text = f"{self.model}, {self.dpi} dpi" if self.dpi else self.model
        if self.max_width_dots:
            mm = self.max_width_dots / {152: 6, 203: 8, 300: 12, 600: 24}.get(
                self.dpi, self.dpi / 25.4)
            text += f", prints up to {mm:.0f} mm wide"
        return text


@dataclass
class PrinterStatus:
    paper_out: bool = False
    paused: bool = False
    head_open: bool = False
    ribbon_out: bool = False
    labels_waiting: bool = False

    def blocking(self) -> List[str]:
        """Problems that stop the printer printing."""
        return [text for flag, text in ((self.paper_out, "out of labels"),
                                        (self.head_open, "the print head is open"),
                                        (self.ribbon_out, "out of ribbon")) if flag]

    def describe(self) -> str:
        notes = self.blocking() + [text for flag, text in (
            (self.paused, "paused"),
            (self.labels_waiting, "a label is waiting to be taken")) if flag]
        return "Ready" if not notes else "; ".join(n[0].upper() + n[1:] for n in notes)


def query(cfg: PrinterConfig, command: bytes, replies: int = 1, timeout: float = 2.0) -> bytes:
    """Send a host query (~HI, ~HS) and read the printer's reply: replies blocks, each
    framed by STX ... ETX. Works over the network and through a USB device file; CUPS
    queues and files can't answer. Raises PrintError."""
    if cfg.method == "network":
        try:
            with socket.create_connection((cfg.host, int(cfg.port)), timeout=timeout) as s:
                s.sendall(command)
                data = b""
                while data.count(b"\x03") < replies:
                    chunk = s.recv(1024)
                    if not chunk:
                        break
                    data += chunk
        except socket.timeout:
            raise PrintError("The printer didn't answer.")
        except OSError as e:
            raise PrintError(f"Could not reach printer at {cfg.host}:{cfg.port}: {e.strerror or e}")
        return data
    if cfg.method == "device":
        import select
        try:
            fd = os.open(cfg.device, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            raise PrintError(f"Could not open {cfg.device}: {e.strerror or e}")
        try:
            os.write(fd, command)
            data, deadline = b"", time.monotonic() + timeout
            while data.count(b"\x03") < replies and time.monotonic() < deadline:
                ready, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
                if not ready:
                    break
                try:
                    chunk = os.read(fd, 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    time.sleep(0.05)
                    continue
                data += chunk
        finally:
            os.close(fd)
        if not data:
            raise PrintError("The printer didn't answer.")
        return data
    raise PrintError("Only network and USB printers can be asked about their state.")


def _blocks(data: bytes) -> List[str]:
    text = data.decode("ascii", errors="replace")
    return [b.split("\x03")[0].strip() for b in text.split("\x02")[1:]]


def parse_info(data: bytes) -> PrinterInfo:
    """~HI reply: model,firmware,dots per mm,memory (e.g. GX420d-200dpi,V61.17.17Z,8,2104KB)."""
    blocks = _blocks(data)
    if not blocks:
        raise PrintError("The printer's answer couldn't be read.")
    parts = [p.strip() for p in blocks[0].split(",")]
    model = re.sub(r"-\d+dpi$", "", parts[0], flags=re.I)
    dpi = 0
    if len(parts) > 2 and parts[2].isdigit():
        dpi = DPMM_TO_DPI.get(int(parts[2]), 0)
    m = re.search(r"(\d+)dpi", parts[0], re.I)
    if not dpi and m:
        dpi = {200: 203}.get(int(m.group(1)), int(m.group(1)))
    width_mm = next((w for prefix, w in sorted(MAX_WIDTH_MM.items(), key=lambda kv: -len(kv[0]))
                     if model.upper().startswith(prefix)), 0)
    dots_per_mm = {152: 6, 203: 8, 300: 12, 600: 24}.get(dpi, dpi / 25.4)
    return PrinterInfo(model, parts[1] if len(parts) > 1 else "", dpi,
                       round(width_mm * dots_per_mm) if dpi and width_mm else 0)


def parse_status(data: bytes) -> PrinterStatus:
    """~HS reply, three blocks: aaa,b,c,... (b paper out, c paused) and mmm,n,o,p,q,r,s,t,...
    (o head open, p ribbon out, q thermal transfer mode, t label waiting)."""
    blocks = _blocks(data)
    if len(blocks) < 2:
        raise PrintError("The printer's status couldn't be read.")
    one, two = blocks[0].split(","), blocks[1].split(",")
    flag = lambda fields, i: len(fields) > i and fields[i].strip() == "1"
    return PrinterStatus(paper_out=flag(one, 1), paused=flag(one, 2), head_open=flag(two, 2),
                         ribbon_out=flag(two, 3) and flag(two, 4), labels_waiting=flag(two, 7))


def identify(cfg: PrinterConfig, timeout: float = 2.0) -> PrinterInfo:
    return parse_info(query(cfg, b"~HI", 1, timeout))


def status(cfg: PrinterConfig, timeout: float = 2.0) -> PrinterStatus:
    return parse_status(query(cfg, b"~HS", 3, timeout))


def calibrate(cfg: PrinterConfig) -> str:
    """Have the printer measure the label stock (it feeds a few labels)."""
    return send("~JC", cfg)


def send(zpl: str, cfg: PrinterConfig, timeout: float = 10.0) -> str:
    """Send ZPL and return a short description of where it went. Raises PrintError."""
    data = zpl.encode("utf-8")
    if cfg.method == "network":
        try:
            with socket.create_connection((cfg.host, int(cfg.port)), timeout=timeout) as s:
                s.sendall(data)
        except OSError as e:
            raise PrintError(f"Could not reach printer at {cfg.host}:{cfg.port}: {e.strerror or e}")
        return f"{cfg.host}:{cfg.port}"
    if cfg.method == "device":
        if not os.path.exists(cfg.device):
            raise PrintError(f"{cfg.device} does not exist. Is the printer plugged in and on?")
        try:
            with open(cfg.device, "wb") as f:
                f.write(data)
        except PermissionError:
            raise PrintError(f"No permission to write {cfg.device}. Add yourself to the "
                             "'lp' group (sudo usermod -aG lp $USER) and log in again.")
        except OSError as e:
            raise PrintError(f"Could not write {cfg.device}: {e.strerror or e}")
        return cfg.device
    if cfg.method == "cups":
        if not cfg.cups_queue:
            raise PrintError("Choose a CUPS printer first.")
        if not shutil.which("lp"):
            raise PrintError("The 'lp' command is not installed (package cups-client).")
        r = subprocess.run(["lp", "-d", cfg.cups_queue, "-o", "raw"], input=data,
                           capture_output=True, timeout=timeout)
        if r.returncode != 0:
            raise PrintError(r.stderr.decode(errors="replace").strip() or "lp failed")
        return f"CUPS queue {cfg.cups_queue}"
    if cfg.method == "file":
        if not cfg.file_path:
            raise PrintError("Choose a file to save to first.")
        with open(cfg.file_path, "wb") as f:
            f.write(data)
        return cfg.file_path
    raise PrintError(f"Unknown print method {cfg.method!r}")
