"""Telegram-értesítés value tippekről (push a telefonra, laptop nélkül is).

Miért jobb az emailnél: nincs SMTP/IMAP/app-jelszó, azonnali push, és a tipp
alatt gombok (✅ Megraktam / ❌ Kihagytam). A felhős figyelő (GitHub Actions)
ugyanezt küldi, így KIKAPCSOLT laptop mellett is jön az értesítés.

Beállítás:
  1. Telegramban írj a @BotFather-nek: /newbot -> kapsz egy TOKEN-t.
  2. Indíts egy beszélgetést a saját botoddal (küldj neki egy /start-ot).
  3. A chat_id lekérése: nyisd meg
     https://api.telegram.org/bot<TOKEN>/getUpdates  és keresd a "chat":{"id":...}.
  4. A TOKEN-t és a chat_id-t a config.json `telegram` blokkjába írd
     (lokálisan), a felhőhöz pedig GitHub secretként: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID.

Csak a Python beépített urllib-jét használja (nincs extra függőség, a felhőben
és lokálisan is megy), és tiszteletben tartja a verify_ssl=false beállítást.
"""
import json
import ssl
import threading
import urllib.parse
import urllib.request

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramNotifier:
    def __init__(self, cfg):
        t = cfg.get("telegram", {})
        self.token = (t.get("token") or "").strip()
        self.chat_id = str(t.get("chat_id") or "").strip()
        self.verify = cfg.get("http", {}).get("verify_ssl", True)

    def configured(self):
        return bool(self.token and self.chat_id
                    and "IDE_JON" not in self.token
                    and "IDE_JON" not in self.chat_id)

    def _ctx(self):
        if self.verify:
            return ssl.create_default_context()
        return ssl._create_unverified_context()

    def _call(self, method, payload):
        url = API.format(token=self.token, method=method)
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=25, context=self._ctx()) as r:
            out = json.loads(r.read().decode("utf-8"))
        if not out.get("ok"):
            raise RuntimeError(f"Telegram API hiba: {out.get('description')}")
        return out.get("result")

    def _get(self, method, params=None):
        url = API.format(token=self.token, method=method)
        if params:
            url += "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=35, context=self._ctx()) as r:
            out = json.loads(r.read().decode("utf-8"))
        if not out.get("ok"):
            raise RuntimeError(f"Telegram API hiba: {out.get('description')}")
        return out.get("result")

    # ---------- küldés ----------
    def send(self, text, buttons=None, chat_id=None):
        if not self.configured():
            raise RuntimeError("Telegram nincs beállítva (telegram.token / chat_id).")
        payload = {
            "chat_id": chat_id or self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        return self._call("sendMessage", payload)

    def send_async(self, text, buttons=None):
        threading.Thread(target=self._safe, args=(text, buttons), daemon=True).start()

    def _safe(self, text, buttons=None):
        try:
            self.send(text, buttons)
            print("[telegram] elküldve")
        except Exception as e:
            print(f"[telegram] HIBA: {e}")

    # ---------- gomb-válaszok beolvasása ----------
    def get_updates(self, offset=None, timeout=0):
        params = {"timeout": timeout,
                  "allowed_updates": json.dumps(["callback_query", "message"])}
        if offset is not None:
            params["offset"] = offset
        return self._get("getUpdates", params)

    def answer_callback(self, callback_id, text=""):
        try:
            self._call("answerCallbackQuery",
                       {"callback_query_id": callback_id, "text": text})
        except Exception:
            pass

    def edit_text(self, chat_id, message_id, text):
        """Üzenet szövegének átírása + gombok eltávolítása (döntés utáni nyom)."""
        try:
            self._call("editMessageText", {
                "chat_id": chat_id, "message_id": message_id, "text": text,
                "parse_mode": "HTML", "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": []},
            })
        except Exception:
            pass


# ---------- üzenet-szöveg ----------
def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def format_tip(d, token=None):
    """Egy tipp Telegram-üzenete (HTML). A token rejtve (spoiler) megy,
    hogy a 'Megraktam' gomb visszafejthető legyen belőle."""
    stake_str = f"{d['stake']:,}".replace(",", " ")
    lines = [
        f"\U0001F7E2 <b>{_esc(d['event'])}</b>",
        f"<i>{_esc(d['sport'])}</i> · {_esc(d['market_name'])}",
        f"\U0001F3AF <b>{_esc(d['tip'])}</b>",
        f"Odds <b>{d['odds']:.2f}</b> · value <b>+{d['value_pct']}%</b>"
        f" · limit ${d.get('limit', 0)}",
        f"\U0001F4B0 Javasolt tét: <b>{stake_str} Ft</b> (tőke {d['stake_pct']}%-a)",
    ]
    if d.get("pinn_url"):
        lines.append(f'<a href="{_esc(d["pinn_url"])}">Pinnacle ellenőrzés</a>')
    if token:
        lines.append(f"<tg-spoiler>{token}</tg-spoiler>")
    return "\n".join(lines)


# A tipp alatti gombok. A visszafejtés a beágyazott tokenből történik (mint az
# emailnél), ezért a callback_data rövid jelölés elég.
BUTTONS = [[
    {"text": "✅ Megraktam", "callback_data": "vbok"},
    {"text": "❌ Kihagytam", "callback_data": "vbno"},
]]
