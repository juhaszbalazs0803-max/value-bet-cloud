"""Value- és Kelly-számítás.

Alapfogalom:  value = valós_valószínűség * odds - 1
Ha pozitív, a fogadóiroda többet fizet, mint amennyit a tényleges esély indokol.

A "valós valószínűséget" a referencia-iroda(k) odds-aiból becsüljük úgy, hogy
eltávolítjuk belőlük a fogadóiroda hasznát (margin / vig).
"""


def implied_probs(odds_list):
    """Decimális odds -> nyers implikált valószínűségek (összegük > 1 a margin miatt)."""
    return [1.0 / o for o in odds_list]


def devig_proportional(odds_list):
    """Margin arányos eltávolítása: p_i = (1/o_i) / sum(1/o)."""
    q = implied_probs(odds_list)
    s = sum(q)
    return [x / s for x in q]


def devig_shin(odds_list, iters=100):
    """Shin-módszer: a favorit-longshot torzítást is figyelembe veszi.

    Visszaadja a torzítatlan valószínűségeket. Két-három kimenetre stabil.
    """
    q = implied_probs(odds_list)
    s = sum(q)
    z = 0.0
    for _ in range(iters):
        probs = [((zsq := (z * z + 4 * (1 - z) * qi * qi / s)) ** 0.5 - z) / (2 * (1 - z))
                 for qi in q]
        new_z = (sum(probs) - 1) / (len(q) - 1) if len(q) > 1 else 0.0
        if abs(new_z - z) < 1e-9:
            z = new_z
            break
        z = max(0.0, min(0.2, new_z))
    probs = [((z * z + 4 * (1 - z) * qi * qi / s) ** 0.5 - z) / (2 * (1 - z)) for qi in q]
    t = sum(probs)
    return [p / t for p in probs]


def fair_probs(odds_list, method="proportional"):
    if method == "shin" and len(odds_list) >= 2:
        try:
            return devig_shin(odds_list)
        except Exception:
            pass
    return devig_proportional(odds_list)


def margin_pct(odds_list):
    """Az iroda margin-ja százalékban (pl. 5.2%)."""
    return (sum(implied_probs(odds_list)) - 1.0) * 100.0


def value_pct(fair_p, dec_odds):
    """Value százalékban: (p*o - 1) * 100."""
    return (fair_p * dec_odds - 1.0) * 100.0


def kelly_fraction(fair_p, dec_odds):
    """Teljes Kelly-tört (a bankroll hányada). b = o-1."""
    b = dec_odds - 1.0
    if b <= 0:
        return 0.0
    f = (b * fair_p - (1.0 - fair_p)) / b
    return max(0.0, f)
