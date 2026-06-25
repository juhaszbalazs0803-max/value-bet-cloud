"""Visszamenőleges végeredmény-lekérés a TheSportsDB ingyenes API-ból.

Miért kell: az Altenar/Pinnacle élő feed a befejezett meccset eldobja, ezért az
`results.py` csak akkor tud lezárni, ha az app FUTOTT a meccs vége környékén. Ez
a modul ELLENBEN visszamenőleg lekéri egy lejátszott meccs végeredményét a
TheSportsDB-ből, így a megrakott (papír) tételeket akkor is le tudjuk zárni, ha
a gép a meccs alatt ki volt kapcsolva.

TheSportsDB:
  - Ingyenes, kulcs nélkül használható teszt-kulccsal ("3").
  - `eventsday.php?d=YYYY-MM-DD&s=<Sport>` -> az adott nap összes meccse a
    sportágban, befejezetteknél `intHomeScore`/`intAwayScore` kitöltve.
  - Foci/kosár/jégkorong végeredményt ad; tenisznél nincs használható pontszám
    (szett/game), azt továbbra is kézzel zárjuk (mint eddig).

A meccset csapatnév-hasonlóság + dátum alapján párosítjuk (ugyanazzal a token-
átfedéses logikával, mint a Pinnacle-párosítás). Best-effort: fő ligákban
megbízható, egzotikus meccsnél None -> marad kézi lezárás.

TENISZ: a `intHomeScore`/`intAwayScore` üres, DE a `strResult` mező tartalmazza
a szett-eredményt és a győztest, pl.:
  "Alcaraz  beat Djokovic  3-0\nAlcaraz : 6 6 7\nDjokovic : 2 2 6"
Ebből kinyerjük a szetteket ÉS a game-eket (a fogadás hazai/vendég tájolásában),
így a tenisz is automatikusan lezárható (lásd `tennis_result` + `results.grade_tennis`).
"""
import re
from datetime import datetime, timedelta

from . import matching

BASE = "https://www.thesportsdb.com/api/v1/json"

# vegas sportId -> TheSportsDB sport-név. Itt a pontszám gól/pont (intHomeScore/
# intAwayScore), ezért az `final_score` közvetlenül használható.
SPORTSDB_SPORT = {
    66: "Soccer",
    67: "Basketball",
    70: "Ice_Hockey",
}

TENNIS_SPORT_ID = 68
TENNIS_NAME = "Tennis"


def _sim(a, b):
    """Csapat/játékos-név hasonlóság a matching token-átfedéssel (0..1)."""
    ta, tb = matching.tokens(a or ""), matching.tokens(b or "")
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _tennis_names(ev):
    """A tenisz-meccs két játékosa a strEvent-ből ('Verseny J1 vs J2').
    Visszaad: (home_str, away_str) vagy (None, None)."""
    s = ev.get("strEvent") or ev.get("strEventAlternate") or ""
    m = re.split(r"\s+vs\.?\s+", s, maxsplit=1, flags=re.I)
    if len(m) != 2:
        return None, None
    return m[0].strip(), m[1].strip()


def parse_tennis_result(str_result, bet_home, bet_away):
    """A TheSportsDB `strResult` mezőjéből (hs_set, as_set, hs_games, as_games),
    a FOGADÁS hazai/vendég tájolásában. None, ha nem értelmezhető.

    Példa bemenet: "Alcaraz  beat Djokovic  3-0\\nAlcaraz : 6 6 7\\nDjokovic : 2 2 6"
    """
    if not str_result:
        return None
    lines = [l.strip() for l in str_result.replace("\r", "").split("\n") if l.strip()]
    if not lines:
        return None
    m = re.search(r"(.+?)\s+beat\s+(.+?)\s+(\d+)\s*[-:]\s*(\d+)", lines[0], re.I)
    if not m:
        return None
    winner = m.group(1).strip()
    ws, ls = int(m.group(3)), int(m.group(4))
    # szettenkénti game-ek játékosonként
    games = {}
    for l in lines[1:]:
        if ":" in l:
            name, _, rest = l.partition(":")
            nums = [int(x) for x in re.findall(r"\d+", rest)]
            if nums:
                games[name.strip()] = sum(nums)
    winner_is_home = _sim(winner, bet_home) >= _sim(winner, bet_away)
    home_sets, away_sets = (ws, ls) if winner_is_home else (ls, ws)
    home_games = away_games = None
    for nm, g in games.items():
        if _sim(nm, bet_home) >= _sim(nm, bet_away):
            home_games = g
        else:
            away_games = g
    return home_sets, away_sets, home_games, away_games


def _norm_team(s):
    return matching.tokens(s or "")


def _score(home_tokens, away_tokens, ev_home, ev_away):
    """Hasonlósági pont egy TheSportsDB-meccsre, MINDKÉT csapatot megkövetelve.
    Visszaad: (pont, swapped) — swapped=True ha a TheSportsDB hazai = a mi vendégünk."""
    eh, ea = _norm_team(ev_home), _norm_team(ev_away)

    def sim(a, b):
        if not a or not b:
            return 0.0
        return len(a & b) / min(len(a), len(b))

    direct = min(sim(home_tokens, eh), sim(away_tokens, ea))
    swapped = min(sim(home_tokens, ea), sim(away_tokens, eh))
    if swapped > direct:
        return swapped, True
    return direct, False


class SportsDBClient:
    # gól/pont-alapú sportok (intHomeScore/intAwayScore): foci/kosár/jégkorong
    SPORTSDB_SPORT_IDS = frozenset(SPORTSDB_SPORT)
    # minden sport, amire van automatikus visszamenőleges lezárás (+ tenisz)
    RETRO_SPORT_IDS = frozenset(SPORTSDB_SPORT) | {TENNIS_SPORT_ID}

    def __init__(self, http, cfg):
        self.http = http
        rcfg = cfg.get("results", {})
        self.key = str(rcfg.get("sportsdb_key", "3"))
        self.min_score = float(rcfg.get("sportsdb_min_score", 0.6))
        # (sport_name, date) -> events lista; egy settle-körön belül cache-elünk,
        # hogy ugyanarra a napra ne kérdezzünk többször.
        self._cache = {}

    def _events_on(self, sport_name, date_str):
        ckey = (sport_name, date_str)
        if ckey in self._cache:
            return self._cache[ckey]
        url = f"{BASE}/{self.key}/eventsday.php"
        try:
            data = self.http.get_json(url, params={"d": date_str, "s": sport_name})
            events = data.get("events") or []
        except Exception:
            events = []
        self._cache[ckey] = events
        return events

    def final_score(self, sport_id, home, away, start_iso):
        """A meccs végeredménye a BET hazai/vendég tájolásában: (hs, as_) vagy None.

        - sport_id: vegas sportId (66/67/70 támogatott)
        - home/away: a fogadás csapatnevei ("Hazai - Vendég" sorrend)
        - start_iso: a meccs kezdése (ISO) -> ebből a naptári nap (+- 1 a tz miatt)
        """
        sport_name = SPORTSDB_SPORT.get(sport_id)
        if not sport_name or not start_iso:
            return None
        try:
            base_day = datetime.fromisoformat(str(start_iso).replace("Z", "+00:00"))
        except Exception:
            return None
        ht, at = _norm_team(home), _norm_team(away)
        if not ht or not at:
            return None

        best = None  # (score, hs, as_)
        # a kezdés napja, valamint az előző/következő nap (időzóna-eltérés miatt)
        for delta in (0, -1, 1):
            d = (base_day + timedelta(days=delta)).strftime("%Y-%m-%d")
            for ev in self._events_on(sport_name, d):
                hs_raw, as_raw = ev.get("intHomeScore"), ev.get("intAwayScore")
                if hs_raw is None or as_raw is None or hs_raw == "" or as_raw == "":
                    continue  # még nincs végeredmény
                score, swapped = _score(ht, at, ev.get("strHomeTeam", ""),
                                        ev.get("strAwayTeam", ""))
                if score < self.min_score:
                    continue
                try:
                    eh, ea = int(hs_raw), int(as_raw)
                except (TypeError, ValueError):
                    continue
                # a fogadás hazai/vendég tájolására igazítjuk
                hs, as_ = (ea, eh) if swapped else (eh, ea)
                if best is None or score > best[0]:
                    best = (score, hs, as_)
        if best:
            return best[1], best[2]
        return None

    def tennis_result(self, home, away, start_iso):
        """Tenisz végeredmény a `strResult`-ból, a BET hazai/vendég tájolásában:
        (home_sets, away_sets, home_games, away_games) vagy None.

        A meccset a két játékos nevére párosítjuk (mindkettőnek egyeznie kell),
        majd a `strResult`-ot szettre+game-re bontjuk (lásd parse_tennis_result)."""
        if not start_iso:
            return None
        try:
            base_day = datetime.fromisoformat(str(start_iso).replace("Z", "+00:00"))
        except Exception:
            return None
        ht, at = _norm_team(home), _norm_team(away)
        if not ht or not at:
            return None
        best = None  # (score, strResult)
        for delta in (0, -1, 1):
            d = (base_day + timedelta(days=delta)).strftime("%Y-%m-%d")
            for ev in self._events_on(TENNIS_NAME, d):
                sr = ev.get("strResult")
                if not sr or "beat" not in sr.lower():
                    continue
                # tenisznél a strHomeTeam/strAwayTeam üres; a játékosnevek a
                # strEvent-ben vannak ("Verseny Játékos1 vs Játékos2")
                ev_home, ev_away = _tennis_names(ev)
                if not ev_home or not ev_away:
                    continue
                score, _ = _score(ht, at, ev_home, ev_away)
                if score < self.min_score:
                    continue
                if best is None or score > best[0]:
                    best = (score, sr)
        if best:
            return parse_tennis_result(best[1], home, away)
        return None
