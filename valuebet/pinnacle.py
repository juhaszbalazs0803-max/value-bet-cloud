"""Pinnacle referencia-odds a publikus guest (arcadia) API-ból.

A Pinnacle a "legélesebb" iroda – az ő odds-aiból becsüljük a valós
valószínűséget. Két végpont kell sportonként:

  matchups:  /0.1/sports/{id}/matchups          -> meccsek (home/away, idő, liga)
  markets:   /0.1/sports/{id}/markets/straight  -> árak (amerikai odds)

A teljes meccs (period 0) piacai:
  s;0;m              meccsgyőztes  -> markets['ml']  {'home','draw','away'}
  s;0;ou;<vonal>     over/under    -> markets['ou']  {vonal: {'over','under'}}
  s;0;s;<vonal>      hendikep      -> markets['ah']  {hazai_vonal: {'home','away'}}

A specialokat (type=special) kihagyjuk. Az árak amerikaiak -> decimálisra váltjuk.
"""
import re
import unicodedata
from datetime import datetime, timezone

API = "https://guest.api.arcadia.pinnacle.com/0.1"
GUEST_KEY = "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R"

# vegas.hu sportId -> Pinnacle sportId
SPORT_MAP = {66: 29, 68: 33, 67: 4, 70: 19, 145: 12, 78: 10, 69: 34}


def american_to_decimal(a):
    a = float(a)
    return round(a / 100.0 + 1.0 if a > 0 else 100.0 / abs(a) + 1.0, 4)


def _parse_dt(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


# Pinnacle sportId -> URL-slug (a kattintható meccs-linkhez)
SPORT_SLUG = {29: "soccer", 33: "tennis", 4: "basketball", 19: "hockey",
              12: "esports", 10: "darts", 34: "volleyball"}


def _slug(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "x"


def _match_url(sport_id, league, home, away, mid):
    """A Pinnacle kanonikus meccs-URL-je. A SPA a végső id-ből tölti be a meccset."""
    sport = SPORT_SLUG.get(sport_id, "soccer")
    return (f"https://www.pinnacle.com/en/{sport}/{_slug(league)}/"
            f"{_slug(home)}-vs-{_slug(away)}/{mid}/#all")


class RefEvent:
    __slots__ = ("id", "home", "away", "start", "league", "markets", "source", "url", "limit")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


class PinnacleClient:
    def __init__(self, http):
        self.http = http
        self.headers = {"X-API-Key": GUEST_KEY, "Referer": "https://www.pinnacle.com/"}

    def _get(self, path):
        return self.http.get_json(API + path, headers=self.headers)

    def fetch_sport(self, pinn_sport_id):
        matchups = self._get(f"/sports/{pinn_sport_id}/matchups?withSpecials=false&brandId=0")
        markets = self._get(f"/sports/{pinn_sport_id}/markets/straight?primaryOnly=false&brandId=0")

        # piacok matchupId szerint csoportosítva (csak period 0)
        by_mu = {}
        for m in markets:
            if m.get("period") != 0:
                continue
            by_mu.setdefault(m.get("matchupId"), []).append(m)

        out = []
        for mu in matchups:
            if mu.get("type") != "matchup" or mu.get("parentId"):
                continue
            if mu.get("isLive"):
                continue  # csak pre-match (az élő odds túl volatilis)
            parts = mu.get("participants", [])
            if len(parts) != 2:
                continue
            home = next((p["name"] for p in parts if p.get("alignment") == "home"), None)
            away = next((p["name"] for p in parts if p.get("alignment") == "away"), None)
            if not home or not away:
                continue
            mkts = self._build_markets(by_mu.get(mu["id"], []))
            if "ml" not in mkts:
                continue
            league = (mu.get("league") or {}).get("name", "")
            out.append(RefEvent(
                id=mu["id"], home=home.strip(), away=away.strip(),
                start=_parse_dt(mu.get("startTime")),
                league=league, markets=mkts, source="pinnacle",
                limit=mkts.pop("_limit", 0),
                url=_match_url(pinn_sport_id, league, home, away, mu["id"]),
            ))
        return out

    @staticmethod
    def _build_markets(markets):
        out = {}
        for m in markets:
            key = m.get("key", "")
            parts = key.split(";")
            if len(parts) < 3:
                continue
            kind = parts[2]
            prices = m.get("prices", [])

            if kind == "m" and "ml" not in out:  # meccsgyőztes
                ml = {}
                for p in prices:
                    d = p.get("designation")
                    if d in ("home", "draw", "away") and p.get("price") is not None:
                        ml[d] = american_to_decimal(p["price"])
                if "home" in ml and "away" in ml:
                    out["ml"] = ml
                    lims = [l.get("amount", 0) for l in m.get("limits", [])
                            if l.get("type") == "maxRiskStake"]
                    out["_limit"] = max(lims) if lims else 0

            elif kind == "ou":  # over/under
                ou = {}
                line = None
                for p in prices:
                    d = p.get("designation")
                    if d in ("over", "under") and p.get("price") is not None:
                        ou[d] = american_to_decimal(p["price"])
                        line = p.get("points")
                if line is not None and "over" in ou and "under" in ou:
                    out.setdefault("ou", {})[round(float(line), 2)] = ou

            elif kind == "s":  # hendikep (spread)
                ah = {}
                home_line = None
                for p in prices:
                    d = p.get("designation")
                    if d in ("home", "away") and p.get("price") is not None:
                        ah[d] = american_to_decimal(p["price"])
                        if d == "home":
                            home_line = p.get("points")
                if home_line is not None and "home" in ah and "away" in ah:
                    out.setdefault("ah", {})[round(float(home_line), 2)] = ah
        return out
