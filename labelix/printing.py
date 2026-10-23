"""Sending ZPL to a printer: network (port 9100), USB device file, CUPS raw queue, or a file."""

from __future__ import annotations

import glob
import os
import shutil
import socket
import subprocess
from dataclasses import asdict, dataclass
from typing import List

METHODS = ("network", "device", "cups", "file")
METHOD_LABELS = {
    "network": "Network (TCP 9100)",
    "device": "USB device file",
    "cups": "CUPS printer (raw)",
    "file": "Save to .zpl file",
}


@dataclass
class PrinterConfig:
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
