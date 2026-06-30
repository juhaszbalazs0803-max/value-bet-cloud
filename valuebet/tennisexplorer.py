"""Visszamenőleges TENISZ-végeredmény a tennisexplorer.com-ról.

Miért: a TheSportsDB ingyenes szintje tenisznél csak a nagy tornákat (ATP/WTA/
Grand Slam) ismeri, a Challenger/ITF/futures mezőny hiányzik. A tennisexplorer
viszont a TELJES tenisz-mezőnyt lefedi (ATP, WTA, Challenger, ITF), és a napi
eredmény-oldala kulcs nélkül, böngészőszerű fejlécekkel elérhető (Cloudflare-
mentes, ellentétben a SofaScore-ral).

Az oldal egy meccset KÉT egymást követő <tr>-ben ad meg:
  <tr id="rN" class="... fRow ...">  -> 1. játékos: t-name + result(szettek) + score-ok(game-ek)
  <tr id="rNb">                       -> 2. játékos: ugyanígy
A győztes a magasabb szettszámú (result td). A nevek "Vezetéknév I." formátumúak,
ezért vezetéknév-token egyezéssel párosítunk a fogadás neveihez.

Kimenet a `result()`-ból a FOGADÁS hazai/vendég tájolásában:
  (home_sets, away_sets, home_games, away_games)  -> a results.grade_tennis kapja.
"""
import html
import re
from datetime import datetime, timedelta

from . import matching

BASE = "https://www.tennisexplorer.com/results/"

# A két sort az id párosítja (rN -> rNb). A `fRow` class CSAK a torna első
# meccsén van, ezért NEM szabad rá szűrni – különben a torna többi meccse (a
# Challenger/ITF mezőny zöme) elveszik és tévesen auto-void lesz.
_MATCH_RE = re.compile(
    r'<tr id="r(\d+)"[^>]*>(.*?)</tr>\s*'
    r'<tr id="r\1b"[^>]*>(.*?)</tr>', re.S)
# /player/ = egyéni, /doubles-team/ = páros (két játékos egy cellában, pl.
# "Arribage / Olivetti") – a párost is el kell fogadni, különben a teljes
# páros-mezőny kimarad és tévesen auto-void lesz.
_NAME_RE = re.compile(r'<td class="t-name"><a href="/(?:player|doubles-team)/[^"]*"[^>]*>([^<]+)</a>')
_RESULT_RE = re.compile(r'<td class="result">(\d+)</td>')
_SCORE_RE = re.compile(r'<td class="score">(\d+)</td>')


def parse_matches(text):
    """A napi eredmény-oldal HTML-jéből: [(név, szettek, [game-ek]), (...)] párok."""
    out = []
    for m in _MATCH_RE.finditer(text):
        r1, r2 = m.group(2), m.group(3)

        def pull(r):
            nm = _NAME_RE.search(r)
            res = _RESULT_RE.search(r)
            if not nm or not res:
                return None
            scores = [int(x) for x in _SCORE_RE.findall(r)]
            return html.unescape(nm.group(1)).strip(), int(res.group(1)), scores

        a, b = pull(r1), pull(r2)
        if a and b:
            out.append((a, b))
    return out


def _sim(a, b):
    ta, tb = matching.tokens(a or ""), matching.tokens(b or "")
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


class TennisExplorerClient:
    def __init__(self, http, cfg):
        self.http = http
        rcfg = cfg.get("results", {})
        self.enabled = bool(rcfg.get("tennisexplorer", True))
        self.min_score = float(rcfg.get("te_min_score", 0.5))
        self._cache = {}   # "YYYY-MM-DD" -> meccslista

    def _matches_on(self, dt):
        key = dt.strftime("%Y-%m-%d")
        if key in self._cache:
            return self._cache[key]
        try:
            text = self.http.get_text(BASE, params={
                "type": "all", "year": dt.year, "month": dt.month, "day": dt.day})
            ms = parse_matches(text)
        except Exception:
            ms = []
        self._cache[key] = ms
        return ms

    def result(self, home, away, start_iso):
        """(home_sets, away_sets, home_games, away_games) a fogadás tájolásában, vagy None."""
        if not self.enabled or not start_iso:
            return None
        try:
            base_day = datetime.fromisoformat(str(start_iso).replace("Z", "+00:00"))
        except Exception:
            return None
        best = None  # (score, a, b, swapped)
        for delta in (0, -1, 1):
            for a, b in self._matches_on(base_day + timedelta(days=delta)):
                pa, pb = a[0], b[0]
                direct = min(_sim(home, pa), _sim(away, pb))
                swapped = min(_sim(home, pb), _sim(away, pa))
                score, sw = (swapped, True) if swapped > direct else (direct, False)
                if score >= self.min_score and (best is None or score > best[0]):
                    best = (score, a, b, sw)
        if not best:
            return None
        _, a, b, sw = best
        if sw:
            a, b = b, a    # most a = hazai, b = vendég
        return a[1], b[1], sum(a[2]), sum(b[2])
