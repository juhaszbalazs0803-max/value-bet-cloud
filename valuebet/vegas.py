"""vegas.hu (Altenar sportsbook) scraper.

A vegas.hu sportfogadását az Altenar rendszere hajtja. A frontend egy JSON
API-ból tölti az odds-okat:
    GET https://hu-sb2frontend-altenar2.biahosted.com/api/widget/GetEvents
        ?culture=hu-HU&timezoneOffset=0&integration=vegas.hu&deviceType=1
        &numFormat=hu-HU&countryCode=US&sportId=<id>&eventCount=0

Egyetlen hívás visszaadja egy sport ÖSSZES előmeccsét: events, markets, odds,
competitors, champs táblákkal. Innen kinyerjük a fő piacokat:
  - meccsgyőztes (1X2 / győztes)        -> markets['ml']  {'home','draw','away'}
  - gólok/pontok száma (Over/Under)     -> markets['ou']  {vonal: {'over','under'}}
  - hendikep                            -> markets['ah']  {hazai_vonal: {'home','away'}}

A piac-típusokat a válasz `headers` listája alapján ismerjük fel (a típus-id-k
sportonként mások), a kimeneteket pedig `competitorId` / odd-név alapján rendeljük.
"""
from datetime import datetime, timezone

_WINNER_NAMES = {"1x2", "győztes", "gyoztes", "meccsgyőztes", "winner", "match winner"}
_OVER_WORDS = ("felett", "över", "over")
_UNDER_WORDS = ("alatt", "under")

SPORT_NAMES = {
    66: "Foci", 67: "Kosárlabda", 68: "Tenisz", 69: "Röplabda",
    70: "Jégkorong", 77: "Asztalitenisz", 78: "Darts", 145: "E-sport",
}


def _parse_dt(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


class VegasEvent:
    __slots__ = ("id", "sport_id", "sport_name", "champ_id", "champ_name",
                 "start", "home", "away", "markets")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def name(self):
        return f"{self.home} - {self.away}"

    @property
    def odds(self):  # visszafelé kompatibilitás (margin-lista)
        return self.markets.get("ml", {})


class VegasClient:
    def __init__(self, http, cfg):
        self.http = http
        self.api_base = cfg["api_base"].rstrip("/") + "/"
        self.common = {
            "culture": cfg.get("culture", "hu-HU"),
            "timezoneOffset": 0,
            "integration": cfg["integration"],
            "deviceType": 1,
            "numFormat": cfg.get("culture", "hu-HU"),
            "countryCode": cfg.get("country_code", "US"),
        }

    def _get(self, endpoint, extra):
        params = dict(self.common)
        params.update(extra)
        return self.http.get_json(self.api_base + endpoint, params=params)

    def fetch_sport(self, sport_id):
        data = self._get("widget/GetEvents", {"sportId": sport_id, "eventCount": 0})
        return list(self._parse(data, sport_id))

    def fetch_live_scores(self, sport_id):
        """Élő meccsek aktuális állása esemény-azonosító szerint (lezáráshoz).

        Visszaad: {event_id: {"score":[hazai,vendég] vagy None, "status":int,
                              "set_score":..., "name":str}}
        A `score[0]` a hazai (competitorIds[0]) — mint a pre-match parse-nál.
        """
        data = self._get("widget/GetLiveEvents", {"sportId": sport_id, "eventCount": 0})
        comp = {c["id"]: c.get("name", "").strip() for c in data.get("competitors", [])}
        out = {}
        for ev in data.get("events", []):
            cids = ev.get("competitorIds") or []
            sc = ev.get("score")
            name = ev.get("name") or (
                f"{comp.get(cids[0], '')} - {comp.get(cids[1], '')}" if len(cids) >= 2 else "")
            out[ev.get("id")] = {
                "score": [sc[0], sc[1]] if isinstance(sc, list) and len(sc) >= 2 else None,
                "set_score": ev.get("currentSetScore"),
                "status": ev.get("status"),
                "name": name,
            }
        return out

    def _parse(self, data, sport_id):
        odd_by_id = {o["id"]: o for o in data.get("odds", [])}
        market_by_id = {m["id"]: m for m in data.get("markets", [])}
        comp_by_id = {c["id"]: c.get("name", "").strip()
                      for c in data.get("competitors", [])}
        champ_by_id = {c["id"]: c.get("name", "").strip()
                       for c in data.get("champs", [])}
        win_t, tot_t, ah_t = self._market_types(data.get("headers", []))
        now = datetime.now(timezone.utc)

        for ev in data.get("events", []):
            comp_ids = ev.get("competitorIds") or []
            if len(comp_ids) < 2:
                continue
            start = _parse_dt(ev.get("startDate"))
            if start and start <= now:
                continue  # már elkezdődött / élő -> kihagyjuk (csak pre-match)
            home = comp_by_id.get(comp_ids[0], "").strip()
            away = comp_by_id.get(comp_ids[1], "").strip()
            if not home or not away:
                continue

            markets = self._extract_markets(ev, market_by_id, odd_by_id,
                                            comp_ids, win_t, tot_t, ah_t)
            if "ml" not in markets:
                continue

            yield VegasEvent(
                id=ev.get("id"), sport_id=sport_id,
                sport_name=SPORT_NAMES.get(sport_id, str(sport_id)),
                champ_id=ev.get("champId"),
                champ_name=champ_by_id.get(ev.get("champId"), ""),
                start=start,
                home=home, away=away, markets=markets,
            )

    @staticmethod
    def _market_types(headers):
        """(győztes, totals, hendikep) típus-id halmazok a headerek nevéből."""
        win, tot, ah = set(), set(), set()
        for h in headers:
            name = h.get("name", "").strip().lower()
            tid = h.get("typeId")
            if name in _WINNER_NAMES:
                win.add(tid)
            elif "szám" in name:            # Gólok/Pontok/Játékok száma
                tot.add(tid)
            elif "hendikep" in name or "handicap" in name:
                ah.add(tid)
        if not win and headers:
            win.add(headers[0].get("typeId"))
        return win, tot, ah

    def _extract_markets(self, ev, market_by_id, odd_by_id, comp_ids, win_t, tot_t, ah_t):
        home_id, away_id = comp_ids[0], comp_ids[1]
        out = {}

        def price(o):
            if not o or not o.get("price") or o.get("oddStatus", 0) != 0:
                return None
            return float(o["price"])

        for mid in ev.get("marketIds", []):
            m = market_by_id.get(mid)
            if not m:
                continue
            tid = m.get("typeId")
            sv = m.get("sv")
            ods = [odd_by_id.get(o) for o in m.get("oddIds", [])]

            # --- meccsgyőztes (nincs vonal) ---
            if tid in win_t and sv is None and "ml" not in out:
                ml = {}
                for o in ods:
                    p = price(o)
                    if p is None:
                        continue
                    cid = (o or {}).get("competitorId")
                    if cid == home_id:
                        ml["home"] = p
                    elif cid == away_id:
                        ml["away"] = p
                    elif cid is None:
                        ml["draw"] = p
                if "home" in ml and "away" in ml:
                    out["ml"] = ml
                continue

            if sv is None:
                continue
            try:
                line = float(str(sv).replace(",", "."))
            except ValueError:
                continue

            # --- Over/Under (vonal, nincs competitorId) ---
            if tid in tot_t:
                ou = {}
                for o in ods:
                    p = price(o)
                    nm = (o or {}).get("name", "").lower()
                    if p is None:
                        continue
                    if any(w in nm for w in _OVER_WORDS):
                        ou["over"] = p
                    elif any(w in nm for w in _UNDER_WORDS):
                        ou["under"] = p
                if "over" in ou and "under" in ou:
                    out.setdefault("ou", {})[line] = ou

            # --- hendikep (competitorId-vel, sv = hazai vonal) ---
            elif tid in ah_t:
                ah = {}
                for o in ods:
                    p = price(o)
                    cid = (o or {}).get("competitorId")
                    if p is None:
                        continue
                    if cid == home_id:
                        ah["home"] = p
                    elif cid == away_id:
                        ah["away"] = p
                if "home" in ah and "away" in ah:
                    out.setdefault("ah", {})[line] = ah

        return out
