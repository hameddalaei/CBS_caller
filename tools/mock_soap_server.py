#!/usr/bin/env python3
"""Development helper: a fake SOAP web service + fake "SFTP / processing" side.

Behaviour per file name received in <fileName>:
  * Until --sftp-delay seconds after the file appeared in the staging dir
    (Linux ctime), it answers with a "File does not exist" SOAP fault (HTTP 500).
  * After that it answers success once and, --process-delay seconds later,
    writes three result files (summary / detail / errors) into the results dir.

Usage:
  python tools/mock_soap_server.py --staging-dir data/staging --results-dir data/results
"""

import argparse
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

FAULT = (
    '<?xml version="1.0"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
    "<soap:Fault><faultcode>soap:Server</faultcode><faultstring>File does not exist: {name}</faultstring>"
    "</soap:Fault></soap:Body></soap:Envelope>"
)
OK = (
    '<?xml version="1.0"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
    "<ProcessFileResponse><ProcessFileResult>OK</ProcessFileResult></ProcessFileResponse>"
    "</soap:Body></soap:Envelope>"
)

args = None
scheduled = set()
lock = threading.Lock()


def write_results(name):
    stem = Path(name).stem
    out = Path(args.results_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{stem}_detail.txt").write_text("step 1 ok\nstep 2 ok\nstep 3 ok\n", encoding="utf-8")
    (out / f"{stem}_errors.txt").write_text("", encoding="utf-8")
    (out / f"{stem}_summary.txt").write_text(f"file={name}\nSTATUS: SUCCESS\n", encoding="utf-8")
    print(f"[mock] wrote result files for {name}", flush=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _reply(self, code, body):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8", "replace")
        m = re.search(r"<(?:[\w.-]+:)?fileName>(.*?)</", body)
        name = m.group(1) if m else ""
        path = Path(args.staging_dir) / name
        arrived = path.exists() and (time.time() - path.stat().st_ctime) >= args.sftp_delay
        if not name or not arrived:
            print(f"[mock] {name!r}: file does not exist yet", flush=True)
            return self._reply(500, FAULT.format(name=name))
        print(f"[mock] {name!r}: success", flush=True)
        with lock:
            if name not in scheduled:
                scheduled.add(name)
                threading.Timer(args.process_delay, write_results, args=(name,)).start()
        return self._reply(200, OK)


def main():
    global args
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--staging-dir", default="data/staging")
    p.add_argument("--results-dir", default="data/results")
    p.add_argument("--sftp-delay", type=float, default=20.0, help="seconds until the file 'arrives' remotely")
    p.add_argument("--process-delay", type=float, default=10.0, help="seconds until result files appear")
    args = p.parse_args()
    print(f"[mock] listening on {args.host}:{args.port}", flush=True)
    HTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
