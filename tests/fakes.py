"""Test doubles: a fake modem (Transport) and a fake Synology Chat webhook."""
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer


class FakeModem:
    def __init__(self):
        self.storages = {"ME": [], "SM": []}
        self.cur = "ME"
        self.sim = "READY"
        self.sent = []          # every command received
        self._out = []
        self.closed = False

    def add(self, storage, pdu):
        self.storages[storage].append(pdu)

    def write(self, data):
        cmd = data.decode().strip()
        self.sent.append(cmd)
        r = "OK"
        if cmd == "AT+CPIN?":
            r = f"+CPIN: {self.sim}\r\n\r\nOK"
        elif cmd.startswith("AT+CPMS="):
            s = cmd.split('"')[1]
            if s not in self.storages:
                r = "+CMS ERROR: 302"
            else:
                self.cur = s
        elif cmd == "AT+CMGL=4":
            parts = []
            for i, p in enumerate(self.storages[self.cur]):
                parts.append(f"+CMGL: {i},1,,{len(p)//2-8}\r\n{p}")
            r = ("\r\n".join(parts) + "\r\n\r\nOK") if parts else "OK"
        self._out.append((r + "\r\n").encode())

    def read(self, timeout_ms):
        return self._out.pop(0) if self._out else b""

    def close(self):
        self.closed = True


class FakeChat:
    """Local HTTP server that mimics the Synology Chat incoming webhook."""

    def __init__(self, fail=0, reject=False):
        self.messages = []
        self.fail = fail        # next N requests -> HTTP 500
        self.reject = reject    # reply {"success": false}
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers["Content-Length"])
                form = urllib.parse.parse_qs(self.rfile.read(n).decode())
                if outer.fail > 0:
                    outer.fail -= 1
                    self.send_response(500); self.end_headers(); return
                outer.messages.append(json.loads(form["payload"][0])["text"])
                body = json.dumps({"success": not outer.reject}).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = (f"http://127.0.0.1:{self.srv.server_port}/webapi/entry.cgi?"
                    "api=SYNO.Chat.External&method=incoming&version=2&token=SECRETTOKEN")
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


class FakeTelegram:
    """Local HTTP server that mimics the Telegram Bot API (sendMessage, getUpdates)."""

    TOKEN = "123456789:" + "A" * 35
    CHAT = "424242"

    def __init__(self, fail=0, reject=False, updates=None):
        self.messages = []      # (title-less) full text sent
        self.bodies = []
        self.paths = []
        self.fail = fail
        self.reject = reject
        self.updates = updates or []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers["Content-Length"])
                form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
                outer.paths.append(self.path)
                if outer.fail > 0:
                    outer.fail -= 1
                    self.send_response(500); self.end_headers(); return
                if self.path.endswith("/getUpdates"):
                    body = {"ok": True, "result": outer.updates}
                else:
                    outer.bodies.append(form)
                    outer.messages.append(form["text"])
                    body = {"ok": not outer.reject}
                self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(body).encode())

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
