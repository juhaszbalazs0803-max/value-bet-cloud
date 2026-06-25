"""Referencia-odds The Odds API-ból (https://the-odds-api.com).

Ingyenes kulcs igényelhető. A több fogadóiroda h2h (meccsgyőztes) odds-aiból
konszenzus valós valószínűséget számolunk: irodánként eltávolítjuk a margint,
majd átlagoljuk. Ez a "sharp" becslés, amihez a vegas.hu odds-ait hasonlítjuk.
"""
from datetime import datetime, timezone

from . import value as V

API_ROOT = "https://api.the-odds-api.com/v4"


def _parse_dt(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


class RefEvent:
    __slots__ = ("home", "away", "start", "fair", "n_books", "sport_key")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


class OddsApiClient:
    def __init__(self, http, cfg):
        self.http = http
        self.key = cfg["oddsapi_key"]
        self.regions = cfg.get("oddsapi_regions", "eu,uk")
        self.sport_keys = cfg.get("oddsapi_sport_keys", [])
        self.devig = cfg.get("devig_method", "proportional")
        self.last_quota = None

    def fetch_reference(self):
        events = []
        for key in self.sport_keys:
            try:
                events.extend(self._fetch_sport(key))
            except Exception as e:
                print(f"  [oddsapi] '{key}' kihagyva: {e}")
        return events

    def _fetch_sport(self, sport_key):
        url = f"{API_ROOT}/sports/{sport_key}/odds"
        params = {
            "apiKey": self.key,
            "regions": self.regions,
            "markets": "h2h",
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        # remaining-request kvóta a válasz fejlécében jön; itt csak a JSON kell
        data = self.http.get_json(url, params=params)
        out = []
        for ev in data:
            re_ = self._parse_event(ev, sport_key)
            if re_:
                out.append(re_)
        return out

    def _parse_event(self, ev, sport_key):
        home = (ev.get("home_team") or "").strip()
        away = (ev.get("away_team") or "").strip()
        if not home or not away:
            return None

        # irodánkénti torzítatlan valószínűségek gyűjtése
        acc = {"home": 0.0, "draw": 0.0, "away": 0.0}
        n = 0
        for bk in ev.get("bookmakers", []):
            market = next((m for m in bk.get("markets", []) if m.get("key") == "h2h"), None)
            if not market:
                continue
            price = {}
            for oc in market.get("outcomes", []):
                nm = oc.get("name", "")
                if nm == home:
                    price["home"] = oc.get("price")
                elif nm == away:
                    price["away"] = oc.get("price")
                elif nm.lower() == "draw":
                    price["draw"] = oc.get("price")
            keys = [k for k in ("home", "draw", "away") if k in price and price[k]]
            if "home" not in price or "away" not in price:
                continue
            probs = V.fair_probs([price[k] for k in keys], self.devig)
            for k, p in zip(keys, probs):
                acc[k] += p
            n += 1

        if n == 0:
            return None
        fair = {k: (acc[k] / n) for k in acc if acc[k] > 0}
        return RefEvent(home=home, away=away, start=_parse_dt(ev.get("commence_time")),
                        fair=fair, n_books=n, sport_key=sport_key)
