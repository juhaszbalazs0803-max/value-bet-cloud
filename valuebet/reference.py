"""Fair-odds referencia-forrás választása a config `reference.provider` alapján.

  - "pinnacle" : Pinnacle guest API (2025.07 óta LEZÁRVA -> jelenleg 503).
  - "smarkets" : Smarkets tőzsde PUBLIKUS, KULCS NÉLKÜLI API-ja (ajánlott
                 pótlék: nulla beállítás, HU-ból is megy). Csak meccsgyőztes.
  - "betfair"  : Betfair Exchange (back/lay közép) – VPN + app key kell.

Egységes felület mindkettőn: `fetch_for_vegas(vegas_sid) -> list[RefEvent] | None`
(None = ez a sport nem támogatott ennél a forrásnál, az engine kihagyja).
"""
import time

from .pinnacle import PinnacleClient, SPORT_MAP as PINN_MAP


class PinnacleReference:
    def __init__(self, http):
        self.client = PinnacleClient(http)
        self.name = "pinnacle"

    def fetch_for_vegas(self, vegas_sid):
        pinn = PINN_MAP.get(vegas_sid)
        if not pinn:
            return None
        return self.client.fetch_sport(pinn)


class SmarketsReference:
    """Smarkets tőzsdei fair odds – KULCS NÉLKÜLI publikus API. Sportonként
    TTL-cache-el, mert az engine 5 mp-enként pörög, de a Smarketst elég
    ~percenként hívni (több köteg-kérés / sport)."""

    def __init__(self, http, cfg):
        from .smarkets import SmarketsRefClient, DOMAIN as SMK_DOMAIN
        self.client = SmarketsRefClient(http)
        self._domain = SMK_DOMAIN
        self.ttl = cfg.get("smarkets", {}).get("cache_ttl_sec", 60)
        self.name = "smarkets"
        self._cache = {}  # vegas_sid -> (ts, events)

    def fetch_for_vegas(self, vegas_sid):
        if vegas_sid not in self._domain:
            return None
        now = time.time()
        c = self._cache.get(vegas_sid)
        if c and now - c[0] < self.ttl:
            return c[1]
        events = self.client.fetch_sport(vegas_sid)
        self._cache[vegas_sid] = (now, events)
        return events


class BetfairReference:
    """A Betfair-lekérést sportonként cache-eli (TTL), mert az engine 5 mp-enként
    pörög, a Betfairnek viszont súlyozott kérés-limitje van – így csak ~percenként
    hívjuk, a köztes ciklusok a cache-ből olvasnak."""

    def __init__(self, http, cfg):
        from .betfair import BetfairRefClient
        self.client = BetfairRefClient(http, cfg)
        self.ttl = cfg.get("betfair", {}).get("cache_ttl_sec", 60)
        self.name = "betfair"
        self._cache = {}  # vegas_sid -> (ts, events)

    def fetch_for_vegas(self, vegas_sid):
        from .betfair import SPORT_MAP as BF_MAP
        if vegas_sid not in BF_MAP:
            return None
        now = time.time()
        c = self._cache.get(vegas_sid)
        if c and now - c[0] < self.ttl:
            return c[1]
        events = self.client.fetch_sport(vegas_sid)
        self._cache[vegas_sid] = (now, events)
        return events


def make_reference(http, cfg):
    provider = (cfg.get("reference", {}).get("provider", "pinnacle") or "").lower()
    if provider == "smarkets":
        return SmarketsReference(http, cfg)
    if provider == "betfair":
        return BetfairReference(http, cfg)
    return PinnacleReference(http)
