"""Fair-odds referencia-forrás választása a config `reference.provider` alapján.

  - "multi"    : TÖBB forrás egyszerre (reference.sources lista, prioritás
                 sorrendben) – több meccs + biztonsági tartalék. AJÁNLOTT.
  - "smarkets" : Smarkets tőzsde PUBLIKUS, KULCS NÉLKÜLI API-ja (sharp).
  - "kambi"    : Kambi/Unibet publikus feed, KULCS NÉLKÜLI (SOFT book, tartalék).
  - "betfair"  : Betfair Exchange (back/lay közép) – VPN + app key kell.
  - "pinnacle" : Pinnacle guest API (jelenleg 503, gyakorlatilag halott).

Egységes felület: `fetch_for_vegas(vegas_sid) -> list[RefEvent] | None`
(None = ezt a sportot egyik forrás sem támogatja; az engine kihagyja).
"""
import time

from .pinnacle import PinnacleClient, SPORT_MAP as PINN_MAP


def _norm(s):
    return "".join(c for c in (s or "").lower() if c.isalnum())


class PinnacleReference:
    def __init__(self, http):
        self.client = PinnacleClient(http)
        self.name = "pinnacle"

    def configured(self):
        return True

    def fetch_for_vegas(self, vegas_sid):
        pinn = PINN_MAP.get(vegas_sid)
        if not pinn:
            return None
        return self.client.fetch_sport(pinn)


class _CachedReference:
    """Közös TTL-cache burok: az engine 5 mp-enként pörög, de a külső forrásokat
    elég ~percenként hívni. A leszármazott a `_client`-et és `_supports`-ot adja."""
    ttl = 60
    name = "?"

    def __init__(self):
        self._cache = {}  # vegas_sid -> (ts, events)

    def configured(self):
        return getattr(self._client, "configured", lambda: True)()

    def _supports(self, vegas_sid):
        raise NotImplementedError

    def fetch_for_vegas(self, vegas_sid):
        if not self._supports(vegas_sid):
            return None
        now = time.time()
        c = self._cache.get(vegas_sid)
        if c and now - c[0] < self.ttl:
            return c[1]
        events = self._client.fetch_sport(vegas_sid)
        self._cache[vegas_sid] = (now, events)
        return events


class SmarketsReference(_CachedReference):
    def __init__(self, http, cfg):
        super().__init__()
        from .smarkets import SmarketsRefClient, DOMAIN
        self._client = SmarketsRefClient(http)
        self._domain = DOMAIN
        self.ttl = cfg.get("smarkets", {}).get("cache_ttl_sec", 60)
        self.name = "smarkets"

    def _supports(self, vegas_sid):
        return vegas_sid in self._domain


class KambiReference(_CachedReference):
    def __init__(self, http, cfg):
        super().__init__()
        from .kambi import KambiRefClient, SPORT_PATH
        self._client = KambiRefClient(http)
        self._paths = SPORT_PATH
        self.ttl = cfg.get("kambi", {}).get("cache_ttl_sec", 60)
        self.name = "kambi"

    def _supports(self, vegas_sid):
        return vegas_sid in self._paths


class BetfairReference(_CachedReference):
    def __init__(self, http, cfg):
        super().__init__()
        from .betfair import BetfairRefClient, SPORT_MAP
        self._client = BetfairRefClient(http, cfg)
        self._map = SPORT_MAP
        self.ttl = cfg.get("betfair", {}).get("cache_ttl_sec", 60)
        self.name = "betfair"

    def _supports(self, vegas_sid):
        return vegas_sid in self._map


_SINGLE = {
    "smarkets": SmarketsReference,
    "kambi": KambiReference,
    "betfair": BetfairReference,
}


def _make_single(http, cfg, name):
    name = name.lower()
    if name == "pinnacle":
        return PinnacleReference(http)
    cls = _SINGLE.get(name)
    return cls(http, cfg) if cls else None


class MultiReference:
    """Több forrás egyszerre, PRIORITÁS sorrendben (a `sources` lista első eleme
    a legerősebb). Egy meccset az első olyan forrás ad, amelyik ismeri; a többi
    csak a HIÁNYZÓ meccseket tölti be -> több tipp. Ha egy forrás kiesik/hibázik,
    a többi viszi tovább -> beépített tartalék (mint amikor a Pinnacle 503 lett).
    A sharp odds nem hígul: az azonos meccsnél a magasabb prioritású (sharp)
    forrás nyer, a soft forrás csak a réseket tölti."""

    def __init__(self, http, cfg):
        names = cfg.get("reference", {}).get("sources") or ["smarkets", "kambi"]
        self.sources = []
        for n in names:
            src = _make_single(http, cfg, n)
            if src and src.configured():
                self.sources.append(src)
        self.name = "multi(" + ",".join(s.name for s in self.sources) + ")"
        self.last_errors = []

    def fetch_for_vegas(self, vegas_sid):
        combined = {}       # norm(home)|norm(away) -> RefEvent (első nyer)
        any_supported = False
        errors = []
        for src in self.sources:
            try:
                evs = src.fetch_for_vegas(vegas_sid)
            except Exception as e:
                errors.append(f"{src.name}: {e}")
                continue
            if evs is None:      # ez a forrás nem ismeri ezt a sportot
                continue
            any_supported = True
            for ev in evs:
                key = _norm(ev.home) + "|" + _norm(ev.away)
                combined.setdefault(key, ev)
        self.last_errors = errors
        if not any_supported and not combined:
            # egyik forrás sem támogatta a sportot ÉS egyik sem adott adatot
            return None if not errors else []
        return list(combined.values())


def make_reference(http, cfg):
    provider = (cfg.get("reference", {}).get("provider", "smarkets") or "").lower()
    if provider == "multi":
        return MultiReference(http, cfg)
    single = _make_single(http, cfg, provider)
    return single if single else PinnacleReference(http)
