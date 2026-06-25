"""Lokális webszerver a felülethez (stdlib, nincs extra függőség).

Végpontok:
  GET  /                -> a felület (index.html)
  GET  /api/state       -> aktuális value betek + meta (JSON)
  GET  /api/settings    -> aktuális szűrők
  POST /api/settings    -> szűrők frissítése (JSON body)
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WEBROOT = os.path.join(os.path.dirname(__file__), "webui")


def make_handler(engine):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass  # csendes

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False)
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/index"):
                with open(os.path.join(WEBROOT, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if self.path == "/api/state":
                return self._send(200, engine.snapshot())
            if self.path == "/api/settings":
                return self._send(200, engine.settings.to_dict())
            return self._send(404, {"error": "not found"})

        def _body(self):
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length) or b"{}")

        def do_POST(self):
            if self.path == "/api/settings":
                engine.settings.update(self._body())
                engine._save()
                return self._send(200, engine.settings.to_dict())
            if self.path == "/api/refresh":
                engine.refresh()
                return self._send(200, {"ok": True})
            if self.path == "/api/notify-test":
                try:
                    engine.notifier.send("✅ Value Bet teszt email",
                                         "Ez egy teszt – az email-értesítés működik.")
                    return self._send(200, {"ok": True})
                except Exception as e:
                    return self._send(200, {"ok": False, "error": str(e)})
            if self.path == "/api/check-inbox":
                try:
                    return self._send(200, engine.check_inbox())
                except Exception as e:
                    return self._send(200, {"ok": False, "reason": str(e)})
            if self.path == "/api/telegram-test":
                try:
                    engine.telegram.send("✅ <b>Value Bet</b> – a Telegram-értesítés működik.")
                    return self._send(200, {"ok": True})
                except Exception as e:
                    return self._send(200, {"ok": False, "error": str(e)})
            if self.path == "/api/check-telegram":
                try:
                    return self._send(200, engine.check_telegram())
                except Exception as e:
                    return self._send(200, {"ok": False, "reason": str(e)})
            if self.path == "/api/check-results":
                try:
                    return self._send(200, engine.check_results())
                except Exception as e:
                    return self._send(200, {"ok": False, "reason": str(e)})
            if self.path == "/api/daily-report":
                try:
                    return self._send(200, engine.send_report_now())
                except Exception as e:
                    return self._send(200, {"ok": False, "reason": str(e)})
            if self.path == "/api/place":
                return self._send(200, engine.place(self._body()))
            if self.path == "/api/settle":
                d = self._body()
                return self._send(200, engine.settle(int(d["id"]), d["result"]) or {"error": "nincs ilyen"})
            if self.path == "/api/delete":
                engine.delete(int(self._body()["id"]))
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})

    return Handler


class _Server(ThreadingHTTPServer):
    # Windows alatt a SO_REUSEADDR engedné, hogy több példány is ugyanarra a portra
    # csatlakozzon (kevert válaszok). Ezt kikapcsoljuk: a második indítás hibázzon.
    allow_reuse_address = False


def serve(engine, host="127.0.0.1", port=8765):
    engine.start()
    httpd = _Server((host, port), make_handler(engine))
    print(f"\n  Felület:  http://{host}:{port}\n  (Leállítás: Ctrl+C)\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nLeállítás...")
        engine.stop()
        httpd.shutdown()
