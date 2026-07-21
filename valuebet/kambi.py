"""Kambi (Unibet) – KULCS NÉLKÜLI tartalék/kiegészítő referencia.

A Kambi egy fogadóiroda-platform (Unibet, 32Red, LeoVegas...) publikus CDN
feeddel — kulcs/regisztráció nélkül. FIGYELEM: ez egy SOFT book (nem tőzsde),
ezért az odds-a kevésbé „éles", mint a Smarketsé. A több-forrású referenciában
(reference.MultiReference) MÁSODLAGOS: a sharp Smarkets tölti be elsőként a
meccseket, a Kambi csak a réseket tölti ki és biztonsági tartalék, ha a
Smarkets kiesne (mint tette a Pinnacle).

Az odds ezredben jön (2500 = 2.50), a fő meccsgyőztes ("Match") piacot vesszük,
csak pre-match (NOT_STARTED) meccsekre.
"""
from datetime import datetime, timezone

from .pinnacle import RefEvent

API_ROOT = "https://eu.offering-api.kambicdn.com/offering/v2018"
BRAND = "ub"  # Unibet

# vegas.hu sportId -> Kambi útvonal
SPORT_PATH = {66: "football", 68: "tennis", 67: "basketball", 70: "ice_hockey"}
OUTCOME_KEY = {"OT_ONE": "home", "OT_CROSS": "draw", "OT_TWO": "away"}


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


class KambiRefClient:
    def __init__(self, http):
        self.http = http

    def configured(self):
        return True  # kulcs nélküli

    def fetch_sport(self, vegas_sid):
        path = SPORT_PATH.get(vegas_sid)
        if not path:
            return None
        url = f"{API_ROOT}/{BRAND}/listView/{path}/all/all/all/matches.json"
        data = self.http.get_json(url, params={
            "lang": "en_GB", "market": "GB", "useCombined": "true"})
        out = []
        for item in data.get("events", []):
            ev = item.get("event", {})
            if ev.get("state") != "NOT_STARTED":
                continue
            home = (ev.get("homeName") or "").strip()
            away = (ev.get("awayName") or "").strip()
            if not home or not away:
                continue
            offer = next((b for b in item.get("betOffers", [])
                          if (b.get("betOfferType") or {}).get("name") == "Match"), None)
            if not offer:
                continue
            ml = {}
            for oc in offer.get("outcomes", []):
                k = OUTCOME_KEY.get(oc.get("type"))
                if k and oc.get("odds"):
                    ml[k] = round(oc["odds"] / 1000.0, 4)
            if "home" not in ml or "away" not in ml:
                continue
            eid = ev.get("id")
            out.append(RefEvent(
                id=eid, home=home, away=away, start=_parse_dt(ev.get("start")),
                league=(ev.get("group") or ""), markets={"ml": ml}, source="kambi",
                url=f"https://www.unibet.com/betting/sports/event/{eid}" if eid else None,
                limit=0,
            ))
        return out
