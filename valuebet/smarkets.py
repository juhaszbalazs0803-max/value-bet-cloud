"""Smarkets Exchange – fair-odds referencia PUBLIKUS, KULCS NÉLKÜLI API-ból.

A Smarkets egy fogadási tőzsde (mint a Betfair): a felhasználók egymás ellen
fogadnak, ezért a piaci ár közel "éles" – gyakorlatilag olyan fair vonal, mint
a (megszűnt) Pinnacle volt. A `https://api.smarkets.com/v3` olvasó végpontjai
NEM igényelnek kulcsot/bejelentkezést, és Magyarországról is elérhetők -> ez a
Pinnacle guest API ingyenes, nulla-beállítás pótléka.

Ár-formátum: a Smarkets "price" a valószínűség bázispontban (pl. 2200 = 22%).
A legjobb bid és a legjobb offer KÖZEPE adja a fair valószínűséget (a három
kimenet közepe ~100%-ra összegződik). fair_odds = 1 / fair_valószínűség.

Jelenleg a MECCSGYŐZTES (winner) piacot adjuk vissza (ml), minden sportra.
"""
from datetime import datetime, timezone

from .pinnacle import RefEvent

API = "https://api.smarkets.com/v3"

# Legfeljebb ekkora bid-offer spread (SZÁZALÉKPONT) mellett fogadjuk el a közepet
# fair árnak. Configból: smarkets.max_spread_pp (None = kapu ki, régi viselkedés).
DEFAULT_MAX_SPREAD_PP = 2.0

# vegas.hu sportId -> (Smarkets type_domain, URL-slug)
DOMAIN = {
    66: ("football", "football"),
    68: ("tennis", "tennis"),
    67: ("basketball", "basketball"),
    70: ("ice_hockey", "ice-hockey"),
}


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _norm(s):
    return "".join(c for c in (s or "").lower() if c.isalnum())


def _mid_odds(quote, max_spread_pp=DEFAULT_MAX_SPREAD_PP):
    """A legjobb bid és offer közepéből fair decimál odds. A Smarkets a bideket
    csökkenő, az offereket növekvő sorrendben adja -> az első elem a legjobb.

    SPREAD-KAPU: a közép csak akkor fair ár, ha a könyv szoros. 2026-08-09-i
    tenisz-mérés: a bid-offer spread MEDIÁNJA 10,8 százalékpont, a kimenetek
    74%-ánál 5pp fölött (a legrosszabb: bid 36,8% / offer 78,7%). Egy 10,8pp
    széles könyv közepe ±5,4pp bizonytalanságú, miközben 1,70-es oddson egy
    3%-os edge mindössze 1,8pp valószínűség -> a mérési zaj a jel többszöröse,
    és épp a legszélesebb (leglikvidebb-ellenes) könyveken gyárt hamis value-t.
    Ezért `max_spread_pp` fölött NEM adunk árat (None) — jobb kihagyni, mint
    zajt fair vonalnak hazudni. Egyoldalú könyv (csak bid vagy csak offer) sem
    ér: ott a spread nem is mérhető.
    """
    bids = quote.get("bids") or []
    offers = quote.get("offers") or []
    bid = (bids[0].get("price") / 10000.0) if bids and bids[0].get("price") else None
    off = (offers[0].get("price") / 10000.0) if offers and offers[0].get("price") else None
    if bid is None or off is None:
        return None, 0.0
    if not (0 < bid < 1) or not (0 < off < 1):
        return None, 0.0
    if max_spread_pp is not None and (off - bid) * 100.0 > max_spread_pp:
        return None, 0.0
    prob = (bid + off) / 2.0
    liq = 0.0
    if offers and offers[0].get("quantity"):
        liq = offers[0]["quantity"] / 10000.0  # tájékoztató likviditás
    return (1.0 / prob if prob > 0 else None), liq


class SmarketsRefClient:
    def __init__(self, http, cfg=None):
        self.http = http
        scfg = (cfg or {}).get("smarkets", {})
        self.max_spread_pp = scfg.get("max_spread_pp", DEFAULT_MAX_SPREAD_PP)

    def _get(self, path):
        return self.http.get_json(API + path)

    def configured(self):
        return True  # kulcs nélküli, mindig kész

    def fetch_sport(self, vegas_sid):
        dom = DOMAIN.get(vegas_sid)
        if not dom:
            return None
        type_domain, slug = dom

        data = self._get(
            f"/events/?type_domain={type_domain}&type_scope=single_event"
            "&state=upcoming&limit=100")
        ev_map = {}
        for e in data.get("events", []):
            name = e.get("name") or ""
            if " vs " not in name:
                continue
            home, _, away = name.partition(" vs ")
            home, away = home.strip(), away.strip()
            if not home or not away:
                continue
            ev_map[e["id"]] = {
                "home": home, "away": away,
                "start": _parse_dt(e.get("start_datetime")),
                "full_slug": e.get("full_slug") or "",
            }
        if not ev_map:
            return []

        # 1) piacok kötegelten -> meccsgyőztes ("winner") piac esemenyenkent
        win_to_event = {}  # market_id -> event_id
        for chunk in _chunks(list(ev_map), 20):
            mk = self._get(f"/events/{','.join(chunk)}/markets/")
            for m in mk.get("markets", []):
                mt = (m.get("market_type") or {}).get("name", "")
                if m.get("slug") == "winner" and mt.startswith("WINNER"):
                    win_to_event[m["id"]] = m.get("event_id")
        if not win_to_event:
            return []
        win_ids = list(win_to_event)

        # 2) kimenetek kötegelten (piaconként a contractok nyers adatai)
        mkt_contracts = {}   # market_id -> [(contract_id, slug, name), ...]
        for chunk in _chunks(win_ids, 50):
            ct = self._get(f"/markets/{','.join(chunk)}/contracts/")
            for c in ct.get("contracts", []):
                mid = c.get("market_id")
                if mid is None:
                    continue
                mkt_contracts.setdefault(mid, []).append(
                    (c["id"], (c.get("slug") or "").lower(), c.get("name") or ""))

        # 3) árak kötegelten (contract_id -> bids/offers)
        quotes = {}
        for chunk in _chunks(win_ids, 50):
            q = self._get(f"/markets/{','.join(chunk)}/quotes/")
            if isinstance(q, dict):
                quotes.update(q)

        # 4) piaconként ml összerakása. Az oldalt a kimenet slugjából (home/draw/
        #    away), vagy ha az a játékos/csapat neve (pl. tenisz), a nevet az
        #    esemény hazai/vendég neveihez illesztve azonosítjuk.
        by_market = {}  # market_id -> {"ml": {...}, "liq": [...]}
        for mid, contracts in mkt_contracts.items():
            eid = win_to_event.get(mid)
            ev = ev_map.get(eid)
            if not ev:
                continue
            hn, an = _norm(ev["home"]), _norm(ev["away"])
            for cid, slug, name in contracts:
                if slug in ("home", "draw", "away"):
                    side = slug
                elif name.strip().lower() in ("draw", "the draw"):
                    side = "draw"
                elif _norm(name) == hn:
                    side = "home"
                elif _norm(name) == an:
                    side = "away"
                else:
                    continue
                q = quotes.get(cid)
                if not q:
                    continue
                odds, liq = _mid_odds(q, self.max_spread_pp)
                if not odds:
                    continue  # hiányzó ár VAGY túl széles könyv -> nincs fair ár
                slot = by_market.setdefault(mid, {"ml": {}, "liq": []})
                slot["ml"][side] = round(odds, 4)
                if side in ("home", "away"):
                    slot["liq"].append(liq)

        out = []
        for mid, slot in by_market.items():
            ml = slot["ml"]
            if "home" not in ml or "away" not in ml:
                continue
            eid = win_to_event.get(mid)
            ev = ev_map.get(eid)
            if not ev:
                continue
            url = f"https://smarkets.com{ev['full_slug']}" if ev["full_slug"] \
                else f"https://smarkets.com/event/{eid}"
            out.append(RefEvent(
                id=eid, home=ev["home"], away=ev["away"], start=ev["start"],
                league="", markets={"ml": ml}, source="smarkets",
                url=url, limit=int(min(slot["liq"])) if slot["liq"] else 0,
            ))
        return out
