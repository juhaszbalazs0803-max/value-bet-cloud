"""Megrakott fogadások automatikus lezárása az Altenar élő feedjéből.

Nincs eredmény-history (a befejezett meccs eltűnik a feedből), ezért az élő
feedet (GetLiveEvents) figyeljük: amíg a meccs él, eltesszük az aktuális állást,
és amikor a meccs eltűnt a feedből (vége) ÉS már rég elkezdődött, a legutoljára
látott állásból lezárjuk.

Konzervatív: amit nem tudunk biztosan eldönteni (pl. tenisz Over/Under, mert a
feed szettben számol, vagy döntetlen ott, ahol nem értelmezhető), azt None-nal
KÉZI lezárásra hagyjuk.
"""

# vegas sportId-k, ahol a 'score' egysége gól/pont -> Over/Under + hendikep értékelhető
TOTAL_OK = {66, 67, 70}      # foci, kosárlabda, jégkorong
# ahol a meccsgyőztesnél lehet döntetlen
DRAW_SPORTS = {66, 70}       # foci, jégkorong

TENNIS_SPORT_ID = 68
# A tenisz ah/ou vonal lehet SZETT- vagy GAME-egységű (vegyesen érkezik a
# könyvelőtől). A vonal nagysága különbözteti meg: szett-vonalak kicsik
# (±0.5..±2.5 ill. total 2.5/3.5), game-vonalak nagyok (±3.5+ ill. total ~15+).
# Ezen küszöb alatt szettnek, felette game-nek vesszük.
TENNIS_SET_LINE_MAX = 2.5    # |ah| <= ez -> szett-hendikep
TENNIS_OU_SET_MAX = 5.5      # ou line <= ez -> szettek összege, felette game-ek


def parse_subkey(subkey):
    """'ml:home' / 'ou:2.5:over' / 'ah:-1.5:home' -> (market, selection, line)."""
    parts = (subkey or "").split(":")
    market = parts[0] if parts else ""
    if market == "ml":
        return "ml", (parts[1] if len(parts) > 1 else ""), None
    if market in ("ou", "ah") and len(parts) >= 3:
        try:
            line = float(parts[1])
        except ValueError:
            line = None
        return market, parts[2], line
    return market, "", None


def grade(sport_id, subkey, home_score, away_score):
    """'won' / 'lost' / 'void' vagy None (nem értékelhető automatikusan)."""
    try:
        hs, as_ = float(home_score), float(away_score)
    except (TypeError, ValueError):
        return None
    market, sel, line = parse_subkey(subkey)

    if market == "ml":
        if hs == as_:
            winner = "draw"
        else:
            winner = "home" if hs > as_ else "away"
        if winner == "draw" and sport_id not in DRAW_SPORTS:
            return None      # döntetlen ott, ahol nem értelmezhető -> kézi
        return "won" if sel == winner else "lost"

    if sport_id not in TOTAL_OK:
        return None          # tenisz/röplabda stb.: ou/ah szettben -> kézi

    if market == "ou" and line is not None:
        total = hs + as_
        if total == line:
            return "void"
        return "won" if ((total > line) == (sel == "over")) else "lost"

    if market == "ah" and line is not None:
        # 'home' vonala = line; 'away' vonala = -line
        margin = (hs + line) - as_ if sel == "home" else (as_ - line) - hs
        if margin == 0:
            return "void"
        return "won" if margin > 0 else "lost"

    return None


def grade_tennis(subkey, home_sets, away_sets, home_games, away_games):
    """Tenisz tipp értékelése: 'won' / 'lost' / 'void' vagy None (kézi).

    - ml: a szettek alapján (teniszben nincs döntetlen).
    - ah: a vonal nagysága szerint SZETT- (|line|<=2.5) vagy GAME-hendikep.
    - ou: a vonal nagysága szerint szettek (<=5.5) vagy game-ek összege.
    """
    market, sel, line = parse_subkey(subkey)
    try:
        hs_set, as_set = int(home_sets), int(away_sets)
    except (TypeError, ValueError):
        return None

    if market == "ml":
        if hs_set == as_set:
            return None
        winner = "home" if hs_set > as_set else "away"
        return "won" if sel == winner else "lost"

    if market == "ah" and line is not None:
        if abs(line) <= TENNIS_SET_LINE_MAX:
            hs, as_ = hs_set, as_set
        else:
            if home_games is None or away_games is None:
                return None
            hs, as_ = int(home_games), int(away_games)
        margin = (hs + line) - as_ if sel == "home" else (as_ - line) - hs
        if margin == 0:
            return "void"
        return "won" if margin > 0 else "lost"

    if market == "ou" and line is not None:
        if line <= TENNIS_OU_SET_MAX:
            total = hs_set + as_set
        else:
            if home_games is None or away_games is None:
                return None
            total = int(home_games) + int(away_games)
        if total == line:
            return "void"
        return "won" if ((total > line) == (sel == "over")) else "lost"

    return None
