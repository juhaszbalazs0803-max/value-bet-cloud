"""Email-értesítés új value tippről (SMTP, pl. Gmail).

Gmailhez „App jelszó" kell (2FA bekapcsolva): https://myaccount.google.com/apppasswords
Ezt írd a config.json -> notify.smtp_password mezőbe (NEM a normál jelszavad).
"""
import smtplib
import ssl
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr


class EmailNotifier:
    def __init__(self, cfg):
        n = cfg.get("notify", {})
        self.host = n.get("smtp_host", "smtp.gmail.com")
        self.port = int(n.get("smtp_port", 587))
        self.user = n.get("smtp_user", "")
        self.password = n.get("smtp_password", "")
        self.to = n.get("to_email") or self.user
        self.from_ = n.get("from_email") or self.user
        self.verify = cfg.get("http", {}).get("verify_ssl", True)

    def configured(self):
        return bool(self.user and self.password and self.to
                    and "IDE_JON" not in self.password)

    def _ctx(self):
        ctx = ssl.create_default_context()
        if not self.verify:  # céges/vírusirtó proxy mögött
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def send(self, subject, body, html=None):
        if not self.configured():
            raise RuntimeError("Email nincs beállítva (notify.smtp_user / smtp_password / to_email).")
        if html:
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText(body, "plain", "utf-8"))
            msg.attach(MIMEText(html, "html", "utf-8"))
        else:
            msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = formataddr(("Value Bet", self.from_))
        msg["To"] = self.to
        with smtplib.SMTP(self.host, self.port, timeout=20) as s:
            s.ehlo()
            s.starttls(context=self._ctx())
            s.login(self.user, self.password)
            s.sendmail(self.from_, [self.to], msg.as_string())

    def send_async(self, subject, body, html=None):
        threading.Thread(target=self._safe, args=(subject, body, html), daemon=True).start()

    def _safe(self, subject, body, html=None):
        try:
            self.send(subject, body, html)
            print(f"[email] elküldve: {subject}")
        except Exception as e:
            print(f"[email] HIBA: {e}")
