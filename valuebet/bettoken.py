"""Fogadás-token az email „Megraktam / Kihagytam" gombjaihoz.

Minden value tipphez egy kompakt, base64-elt adatsort teszünk az emailbe.
A „Megraktam" gomb egy válasz-emailt nyit, aminek a törzsében ott marad ez a
kód. A helyi IMAP-olvasó (inbox.py) ebből állítja vissza a fogadást és menti el.

A base64 (urlsafe, padding nélkül) ellenáll az email-idézésnek és a
quoted-printable sortöréseknek: az olvasó dekódolás előtt minden nem-base64
karaktert (szóköz, „>", sortörés) kidob, majd visszapótolja a paddinget.
"""
import base64
import json
import re
from urllib.parse import quote

PREFIX = "VBDATA:"
SUFFIX = ":ENDVB"

# A megrakott rekord visszaállításához szükséges mezők.
FIELDS = ("key", "sport", "event", "market", "market_name", "tip",
          "odds", "stake", "value_pct", "fair_pct", "start")

_TOKEN_RE = re.compile(re.escape(PREFIX) + r"(.*?)" + re.escape(SUFFIX), re.S)


def encode(bet):
    data = {k: bet.get(k) for k in FIELDS}
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def token_block(bet):
    return f"{PREFIX}{encode(bet)}{SUFFIX}"


def decode(text):
    """Egy email-törzsből visszafejti a fogadás-dictet (vagy None)."""
    m = _TOKEN_RE.search(text or "")
    body = m.group(1) if m else (text or "")
    cleaned = "".join(ch for ch in body if ch.isalnum() or ch in "-_")
    if not cleaned:
        return None
    cleaned += "=" * (-len(cleaned) % 4)
    try:
        raw = base64.urlsafe_b64decode(cleaned)
        d = json.loads(raw.decode("utf-8"))
        return d if isinstance(d, dict) and d.get("key") else None
    except Exception:
        return None


def _mailto(to_email, subject, body):
    return f"mailto:{to_email}?subject={quote(subject)}&body={quote(body)}"


def placed_mailto(to_email, bet):
    body = "Megraktam ezt a tétet. (A lenti kódot ne töröld – ebből jegyzi meg a figyelő.)\n\n" + token_block(bet)
    return _mailto(to_email, "VB OK", body)


def skip_mailto(to_email, bet):
    body = "Kihagytam ezt a tétet.\n\n" + token_block(bet)
    return _mailto(to_email, "VB NO", body)
