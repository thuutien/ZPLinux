#!/usr/bin/env python3
"""A stand-in for a networked Zebra printer, for testing without hardware.

Listens on TCP port 9100 (like a real printer), saves every job it receives as a
.zpl file, and with --render also asks the Labelary web service to turn each job
into a PNG so you can see what a real printer would print. It answers the ~HI
(model) and ~HS (status) queries like a GK420d, and --state makes it report a
problem.

Usage:
    python3 tools/fake_printer.py [--port 9100] [--out received] [--render]
                                  [--dpmm 8] [--size 4x6] [--model GK420d]
                                  [--state ready|paper-out|head-open|paused]
"""

import argparse
import datetime
import os
import socket
import sys
import urllib.error
import urllib.request

LABELARY = "http://api.labelary.com/v1/printers/{dpmm}dpmm/labels/{size}/{index}/"


def render(zpl: bytes, dpmm: int, size: str, out_base: str) -> None:
    labels = zpl.count(b"^XA") or 1
    for i in range(min(labels, 50)):
        req = urllib.request.Request(LABELARY.format(dpmm=dpmm, size=size, index=i), data=zpl,
                                     headers={"Accept": "image/png"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                png = r.read()
        except urllib.error.HTTPError as e:
            print(f"  Labelary error: {e.code} {e.read().decode(errors='replace')[:200]}")
            return
        except OSError as e:
            print(f"  Labelary unreachable: {e}")
            return
        path = f"{out_base}-{i + 1}.png" if labels > 1 else f"{out_base}.png"
        with open(path, "wb") as f:
            f.write(png)
        print(f"  rendered {path}")


def answer(query: bytes, model: str, dpmm: int, state: str) -> bytes:
    """Replies to host queries, framed by STX and ETX like a real printer's."""
    if query == b"~HI":
        dpi = {6: 152, 8: 200, 12: 300, 24: 600}.get(dpmm, 200)
        return f"\x02{model}-{dpi}dpi,V61.17.17Z,{dpmm},2104KB\x03\r\n".encode()
    if query == b"~HS":
        paper, paused = int(state == "paper-out"), int(state == "paused")
        head = int(state == "head-open")
        return (f"\x02030,{paper},{paused},1218,000,0,0,0,000,0,0,0\x03\r\n"
                f"\x02000,0,{head},0,0,2,6,0,00000000,1,000\x03\r\n"
                f"\x021234,0\x03\r\n").encode()
    return b""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--out", default="received", help="folder for received jobs")
    ap.add_argument("--render", action="store_true",
                    help="render each job to PNG with the online Labelary service")
    ap.add_argument("--dpmm", type=int, default=8, help="8 = 203 dpi, 12 = 300 dpi, 24 = 600 dpi")
    ap.add_argument("--size", default="4x6", help="label size in inches for --render, e.g. 4x6")
    ap.add_argument("--model", default="GK420d", help="model to report to ~HI")
    ap.add_argument("--state", default="ready",
                    choices=("ready", "paper-out", "head-open", "paused"),
                    help="status to report to ~HS")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((args.host, args.port))
    except OSError as e:
        print(f"Cannot listen on {args.host}:{args.port}: {e.strerror}")
        return 1
    srv.listen(5)
    print(f"Fake Zebra printer listening on {args.host}:{args.port}, saving jobs to {args.out}/")
    print("Press Ctrl+C to stop.")
    n = 0
    try:
        while True:
            conn, addr = srv.accept()
            with conn:
                conn.settimeout(5)
                chunks = []
                answered = 0
                try:
                    while True:
                        b = conn.recv(65536)
                        if not b:
                            break
                        chunks.append(b)
                        # answer the queries in what has arrived so far
                        seen = b"".join(chunks)
                        queries = [q.encode() for q in ("~HI", "~HS") for _ in
                                   range(seen.count(q.encode()))]
                        for q in sorted(queries, key=lambda q: seen.index(q))[answered:]:
                            conn.sendall(answer(q, args.model, args.dpmm, args.state))
                            answered += 1
                except socket.timeout:
                    pass
            data = b"".join(chunks)
            for q in (b"~HI", b"~HS"):
                data = data.replace(q, b"")
            if b"~JC" in data:
                print(f"Calibrate from {addr[0]} (a real printer would feed a few labels)")
                data = data.replace(b"~JC", b"")
            if not data.strip():
                continue
            n += 1
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            base = os.path.join(args.out, f"job-{stamp}-{n}")
            with open(base + ".zpl", "wb") as f:
                f.write(data)
            print(f"Job {n} from {addr[0]}: {len(data)} bytes, "
                  f"{data.count(b'^XA')} label(s) -> {base}.zpl")
            if args.render:
                render(data, args.dpmm, args.size, base)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        srv.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
