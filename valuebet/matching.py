"""Meccs-párosítás a vegas.hu és a referencia-forrás között.

A két forrás más nyelven írja a csapatneveket (magyar vs. angol), ezért
név-normalizálás + token-átfedés + kezdési idő alapján párosítunk.
A párosítás "best-effort" – fő ligákban megbízható, egzotikus meccseknél
érdemes kézzel ellenőrizni fogadás előtt.
"""
import re
import unicodedata

# Gyakori szavak, amik nem segítik a párosítást.
_STOP = {
    "fc", "cf", "sc", "ac", "as", "if", "sk", "bk", "fk", "club", "club.",
    "u19", "u20", "u21", "u23", "women", "noi", "ladies", "reserves", "ii",
    "the", "de", "city", "town", "united", "utd", "calcio",
}

_ALIASES = {
    "munchen": "munich", "muenchen": "munich",
    "koln": "cologne", "wien": "vienna",
    "torino": "turin", "milano": "milan", "roma": "rome",
    "praha": "prague", "moszkva": "moscow",
}


def normalize(name):
    name = name.lower().strip()
    # ékezetek eltávolítása
    name = "".join(c for c in unicodedata.normalize("NFKD", name)
                   if not unicodedata.combining(c))
    name = re.sub(r"[^a-z0-9 ]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def tokens(name):
    out = set()
    for t in normalize(name).split():
        t = _ALIASES.get(t, t)
        if t and t not in _STOP and len(t) > 1:
            out.add(t)
    return out


def _team_score(a, b):
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return inter / min(len(ta), len(tb))


def pair_score(vegas_home, vegas_away, ref_home, ref_away):
    """A két meccs hasonlósági pontja és tájolása.

    Visszaad: (átlag_pont, cserélt-e, gyengébb_oldal_pontja). A gyengébb oldal
    pontja azért kell, hogy MINDKÉT csapatnak egyeznie kelljen — különben egyetlen
    véletlen token-egyezés is hamis párosítást adna (és irreális value-t).
    """
    d1, d2 = _team_score(vegas_home, ref_home), _team_score(vegas_away, ref_away)
    s1, s2 = _team_score(vegas_home, ref_away), _team_score(vegas_away, ref_home)
    direct, swapped = (d1 + d2) / 2, (s1 + s2) / 2
    if swapped > direct:
        return swapped, True, min(s1, s2)
    return direct, False, min(d1, d2)


def match_events(vegas_events, ref_events, max_start_diff_min=90, min_score=0.6):
    """Minden vegas meccshez megkeresi a legjobb referencia-párt.

    Mindkét csapatnak el kell érnie a min_score-t (nem csak az átlagnak), így
    nem keletkeznek hamis párosítások.
    Visszaad: (vegas_event, ref_event, swapped, score) négyesek.
    """
    pairs = []
    used = set()
    for ve in vegas_events:
        best = None
        for i, re_ in enumerate(ref_events):
            if i in used:
                continue
            if ve.start and re_.start:
                diff = abs((ve.start - re_.start).total_seconds()) / 60.0
                if diff > max_start_diff_min:
                    continue
            score, swapped, weak = pair_score(ve.home, ve.away, re_.home, re_.away)
            if weak >= min_score and score >= min_score and (best is None or score > best[0]):
                best = (score, i, re_, swapped)
        if best:
            used.add(best[1])
            pairs.append((ve, best[2], best[3], round(best[0], 2)))
    return pairs
