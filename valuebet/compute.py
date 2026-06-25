"""Value betek számítása egy párosított meccsre, minden piacon.

Bemenet: egy vegas meccs, a hozzá párosított Pinnacle meccs, és hogy a két
oldal cserélve van-e (swapped). Kimenet: value bet dict-ek listája.

Piacok: meccsgyőztes (ml), Over/Under (ou), hendikep (ah).
"""
from . import value as V

_SWAP = {"home": "away", "away": "home", "draw": "draw"}


def _fair(odds_map, keys, method):
    """Decimális odds-okból torzítatlan valószínűségek a megadott kulcsokra."""
    probs = V.fair_probs([odds_map[k] for k in keys], method)
    return dict(zip(keys, probs))


def _fmt_line(x):
    s = f"{x:+.2f}".rstrip("0").rstrip(".")
    return s if s not in ("+0", "-0") else "0"


def compute_bets(ve, re_, swapped, method="proportional"):
    bets = []
    vm, rm = ve.markets, re_.markets
    url = getattr(re_, "url", None)
    limit = getattr(re_, "limit", 0)

    # ---------- Meccsgyőztes (1X2 / győztes) ----------
    if "ml" in vm and "ml" in rm:
        rkeys = [k for k in ("home", "draw", "away") if k in rm["ml"]]
        fair = _fair(rm["ml"], rkeys, method)
        for label in ("home", "draw", "away"):
            if label not in vm["ml"]:
                continue
            rk = _SWAP[label] if swapped else label
            if rk not in fair:
                continue
            tip = {"home": f"1 — {ve.home}", "draw": "X — döntetlen",
                   "away": f"2 — {ve.away}"}[label]
            bets.append(_bet("ml", f"ml:{label}", "Meccsgyőztes", tip,
                             vm["ml"][label], rm["ml"][rk], fair[rk]))

    # ---------- Over / Under (orientáció-független) ----------
    if "ou" in vm and "ou" in rm:
        for line, vo in vm["ou"].items():
            ro = rm["ou"].get(line)
            if not ro:
                continue
            fair = _fair(ro, ["over", "under"], method)
            for side in ("over", "under"):
                tip = ("Több mint" if side == "over" else "Kevesebb mint") + f" {line:g}"
                bets.append(_bet("ou", f"ou:{line}:{side}", f"O/U {line:g}", tip,
                                 vo[side], ro[side], fair[side]))

    # ---------- Hendikep ----------
    if "ah" in vm and "ah" in rm:
        for hl, vo in vm["ah"].items():
            if not swapped:
                ro = rm["ah"].get(round(hl, 2))
                if not ro:
                    continue
                fair = _fair(ro, ["home", "away"], method)
                fh, fa = fair["home"], fair["away"]
                rh, ra = ro["home"], ro["away"]
            else:
                ro = rm["ah"].get(round(-hl, 2))
                if not ro:
                    continue
                fair = _fair(ro, ["home", "away"], method)
                # vegas hazai <-> pinnacle vendég, vegas vendég <-> pinnacle hazai
                fh, fa = fair["away"], fair["home"]
                rh, ra = ro["away"], ro["home"]
            if "home" in vo:
                bets.append(_bet("ah", f"ah:{hl}:home", f"Hendikep {_fmt_line(hl)}",
                                 f"Hendikep {ve.home} ({_fmt_line(hl)})", vo["home"], rh, fh))
            if "away" in vo:
                bets.append(_bet("ah", f"ah:{hl}:away", f"Hendikep {_fmt_line(-hl)}",
                                 f"Hendikep {ve.away} ({_fmt_line(-hl)})", vo["away"], ra, fa))

    for b in bets:
        b["pinn_url"] = url
        b["limit"] = limit
    return bets


def _bet(market, subkey, market_name, tip, odds, ref_odds, fair_p):
    return {
        "market": market, "subkey": subkey, "market_name": market_name, "tip": tip,
        "odds": round(odds, 3),
        "ref_odds": round(ref_odds, 3),            # nyers Pinnacle odds (viggel)
        "fair_odds": round(1.0 / fair_p, 3),       # vig nélküli (valós) odds
        "fair_pct": round(fair_p * 100, 1),
        "value_pct": round(V.value_pct(fair_p, odds), 2),
        "fair_p": fair_p,
    }
