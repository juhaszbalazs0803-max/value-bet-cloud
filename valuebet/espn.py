"""Kosárlabda-végeredmények az ESPN rejtett scoreboard API-jából.

Miért kell: a TheSportsDB ingyenes szintje kosárban naponta csak néhány meccset
ad vissza (pl. 2026-07-14-re összesen 3-at), ezért a WNBA/NBA Summer League
tippek zöme tévesen auto-void lett. Az ESPN scoreboard ingyenes, kulcs nélküli,
stabil JSON:
    GET https://site.api.espn.com/apis/site/v2/sports/basketball/<liga>/scoreboard
        ?dates=YYYYMMDD
Ligák: wnba, nba, nba-summer-las-vegas (a Summer League NEM az nba slugon van).

Egzotikus ligákat (PBA, uruguayi, új-zélandi NBL) az ESPN sem fed le — azok
továbbra is a TheSportsDB-re, végső soron az auto-void-ra maradnak.

A párosítás ugyanaz a token-átfedéses, mindkét-csapatos logika, mint a
TheSportsDB-nél (sportsdb._score), a fogadás hazai/vendég tájolására igazítva.
"""
from datetime import datetime, timedelta

from .sportsdb import _norm_team, _score

BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball"

BASKETBALL_SPORT_ID = 67

DEFAULT_LEAGUES = ("wnba", "nba", "nba-summer-las-vegas")


class ESPNClient:
    def __init__(self, http, cfg):
        self.http = http
        rcfg = cfg.get("results", {})
        self.leagues = list(rcfg.get("espn_leagues", DEFAULT_LEAGUES))
        self.min_score = float(rcfg.get("espn_min_score",
                                        rcfg.get("sportsdb_min_score", 0.6)))
        # (liga, nap) -> events; egy settle-körön belül nem kérdezzük kétszer
        self._cache = {}

    def _events_on(self, league, yyyymmdd):
        ckey = (league, yyyymmdd)
        if ckey in self._cache:
            return self._cache[ckey]
        try:
            data = self.http.get_json(f"{BASE}/{league}/scoreboard",
                                      params={"dates": yyyymmdd})
            events = data.get("events") or []
        except Exception:
            events = []
        self._cache[ckey] = events
        return events

    @staticmethod
    def _final(ev):
        """(home_név, away_név, home_pont, away_pont) egy BEFEJEZETT ESPN-
        eseményből, különben None."""
        comp = (ev.get("competitions") or [{}])[0]
        st = ((comp.get("status") or ev.get("status") or {}).get("type") or {})
        if not st.get("completed"):
            return None
        home = away = None
        for c in comp.get("competitors", []):
            side = c.get("homeAway")
            name = (c.get("team") or {}).get("displayName", "")
            try:
                pts = int(c.get("score"))
            except (TypeError, ValueError):
                return None
            if side == "home":
                home = (name, pts)
            elif side == "away":
                away = (name, pts)
        if not home or not away:
            return None
        return home[0], away[0], home[1], away[1]

    def final_score(self, home, away, start_iso):
        """A meccs végeredménye a BET hazai/vendég tájolásában: (hs, as_) vagy None."""
        if not start_iso:
            return None
        try:
            base_day = datetime.fromisoformat(str(start_iso).replace("Z", "+00:00"))
        except Exception:
            return None
        ht, at = _norm_team(home), _norm_team(away)
        if not ht or not at:
            return None

        best = None  # (pont, hs, as_)
        # az ESPN dates paramétere US-naptári nap -> a kezdés napja +- 1
        for delta in (0, -1, 1):
            d = (base_day + timedelta(days=delta)).strftime("%Y%m%d")
            for league in self.leagues:
                for ev in self._events_on(league, d):
                    fin = self._final(ev)
                    if not fin:
                        continue
                    ev_home, ev_away, eh, ea = fin
                    score, swapped = _score(ht, at, ev_home, ev_away)
                    if score < self.min_score:
                        continue
                    hs, as_ = (ea, eh) if swapped else (eh, ea)
                    if best is None or score > best[0]:
                        best = (score, hs, as_)
        if best:
            return best[1], best[2]
        return None
