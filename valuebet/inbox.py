"""Gmail IMAP olvasó: a „Megraktam / Kihagytam" válasz-emaileket dolgozza fel.

A „Megraktam" (tárgy: VB OK) válasz törzsében ott a base64 fogadás-token
(bettoken). Ezt visszafejtjük, és a hívó (engine) elmenti a backendbe. A
feldolgozott leveleket olvasottra (Seen) állítjuk.

A belépés a notify.smtp_user / smtp_password (Gmail APP-jelszó) adatokkal megy
— ugyanaz, amivel az emailt küldjük. Tedd a config.json `notify` blokkjába.
(A felhős repóba NEM kerül, mert a config.json a .gitignore-ban van.)

Megjegyzés: az újrafeldolgozás ártalmatlan, mert az engine a `key`-re deduplikál.
"""
import email
import email.header
import imaplib
import ssl
from datetime import datetime, timedelta

from . import bettoken


def _password_ok(pw):
    return bool(pw) and "IDE_JON" not in pw


def _decode_part(part):
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, "replace")
    except Exception:
        return payload.decode("utf-8", "replace")


def _text_body(msg):
    if msg.is_multipart():
        for ctype in ("text/plain", "text/html"):
            for part in msg.walk():
                if part.get_content_type() == ctype:
                    return _decode_part(part)
        return ""
    return _decode_part(msg)


def _subject(msg):
    raw = msg.get("Subject", "")
    try:
        return str(email.header.make_header(email.header.decode_header(raw)))
    except Exception:
        return raw


class InboxReader:
    def __init__(self, cfg):
        n = cfg.get("notify", {})
        ib = cfg.get("inbox", {})
        self.host = ib.get("imap_host", "imap.gmail.com")
        self.port = int(ib.get("imap_port", 993))
        self.since_days = int(ib.get("since_days", 7))
        self.user = n.get("smtp_user", "")
        self.password = n.get("smtp_password", "")
        self.verify = cfg.get("http", {}).get("verify_ssl", True)

    def configured(self):
        return bool(self.user) and _password_ok(self.password)

    def _ctx(self):
        ctx = ssl.create_default_context()
        if not self.verify:           # vírusirtó/proxy MITM mögött
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def fetch_actions(self):
        """[{'action':'ok'|'no', 'bet':dict, 'subject':str}] a friss válaszokból."""
        if not self.configured():
            raise RuntimeError("IMAP nincs beállítva (notify.smtp_user / smtp_password).")
        out = []
        M = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=self._ctx())
        try:
            M.login(self.user, self.password)
            M.select("INBOX")
            since = (datetime.utcnow() - timedelta(days=self.since_days)).strftime("%d-%b-%Y")
            typ, data = M.search(None, f'(SUBJECT "VB " SINCE {since})')
            ids = data[0].split() if (typ == "OK" and data and data[0]) else []
            for num in ids:
                typ, md = M.fetch(num, "(RFC822)")
                if typ != "OK" or not md or not md[0]:
                    continue
                msg = email.message_from_bytes(md[0][1])
                subj = _subject(msg).upper()
                action = "ok" if "VB OK" in subj else ("no" if "VB NO" in subj else None)
                bet = bettoken.decode(_text_body(msg))
                if action and bet:
                    out.append({"action": action, "bet": bet, "subject": subj})
                M.store(num, "+FLAGS", "\\Seen")
        finally:
            try:
                M.logout()
            except Exception:
                pass
        return out
