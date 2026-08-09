"""Fair-odds referencia-forrás választása a config `reference.provider` alapján.

  - "multi"    : TÖBB forrás PRIORITÁS sorrendben (reference.sources lista) –
                 az ELSŐ a fair vonal, a többi csak akkor jut szóhoz, ha az adott
                 sportot az első nem ismeri. AJÁNLOTT.
  - "pinnacle" : Pinnacle guest API – éles iroda, ez a fair vonal alapesetben.
  - "smarkets" : Smarkets tőzsde PUBLIKUS, KULCS NÉLKÜLI API-ja. CSAK szoros
                 könyvvel használható (lásd smarkets.max_spread_pp).
  - "kambi"    : Kambi/Unibet publikus feed – SOFT bukméker, fair vonalnak
                 ALKALMATLAN, alapból tiltott (SOFT_SOURCES / allow_soft).
  - "betfair"  : Betfair Exchange (back/lay közép) – VPN + app key kell.

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

    def _supports(self, vegas_sid):
        return vegas_sid in PINN_MAP

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
        self._client = SmarketsRefClient(http, cfg)
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

# SOFT bukmékerek: a saját áruk NEM fair vonal (be van építve a haszonkulcsuk és
# a saját torzításuk), ezért referenciaként hamis value-t gyártanak. 2026-08-09-i
# mérés: a Smarkets 0 kosárlabda-eseményt ad, így a kosaras fair vonal 100%-ban a
# Kambi (Unibet) volt; teniszben az események ~1/3-át is az adta. A foci-feedje
# ráadásul részben esport ("Torino (T3RZ) - Bologna FC (abr4m_5)").
# Alapból tiltjuk; csak a `reference.allow_soft: true` engedi vissza.
SOFT_SOURCES = {"kambi"}


def _make_single(http, cfg, name):
    name = name.lower()
    if name == "pinnacle":
        return PinnacleReference(http)
    cls = _SINGLE.get(name)
    return cls(http, cfg) if cls else None


class ReferenceDown(Exception):
    """Az adott sport ELSŐDLEGES fair-forrása nem elérhető. A hívó ilyenkor
    HAGYJA KI a sportot és riasszon — tilos gyengébb forrásra visszaesni."""


class MultiReference:
    """Több forrás PRIORITÁS sorrendben (a `sources` lista első eleme a fair vonal).

    SZIGORÚ mód (alap, `reference.strict: true`): egy sportot az első olyan forrás
    ad, amelyik ISMERI a sportot. Ha az elhasal, a kör NEM esik vissza a gyengébb
    forrásra, hanem `ReferenceDown`-t dob -> a hívó kihagyja a sportot és riaszt.

    Miért: 2026-07-21-én a Pinnacle átmenetileg 503-at adott, a csendes fallback
    Smarkets+Kambira váltott, és onnantól hetekig soft/illikvid vonalhoz mérte a
    value-t (a kalibráció elromlott: 70-80%-os sávban várás 74,1% / tény 44,2%).
    Egy múló hiba tartós, észrevétlen minőségromlássá vált. Jobb egy körre vakon
    maradni és szólni, mint rossz vonalról fogadni.

    `reference.fill_gaps` (alap false): engedi-e, hogy alacsonyabb prioritású
    forrás olyan MECCSEKET is hozzáadjon, amiket az elsődleges nem ismer. Alapból
    tiltott — pont ez a rés engedte be a Kambit a tenisz ~1/3-ára."""

    def __init__(self, http, cfg):
        rcfg = cfg.get("reference", {})
        names = rcfg.get("sources") or ["pinnacle", "smarkets"]
        allow_soft = bool(rcfg.get("allow_soft", False))
        self.sources = []
        self.skipped_soft = []
        for n in names:
            if not allow_soft and n.lower() in SOFT_SOURCES:
                self.skipped_soft.append(n.lower())
                print(f"[reference] '{n}' SOFT bukméker -> kihagyva a fair vonalból "
                      "(reference.allow_soft: true engedi vissza)")
                continue
            src = _make_single(http, cfg, n)
            if src and src.configured():
                self.sources.append(src)
        self.name = "multi(" + ",".join(s.name for s in self.sources) + ")"
        self.strict = bool(rcfg.get("strict", True))
        self.fill_gaps = bool(rcfg.get("fill_gaps", False))
        self.last_errors = []

    def fetch_for_vegas(self, vegas_sid):
        combined = {}       # norm(home)|norm(away) -> RefEvent (első nyer)
        any_supported = False
        errors = []
        primary = None      # az első forrás, amelyik ISMERI ezt a sportot
        for src in self.sources:
            supports = src._supports(vegas_sid) if hasattr(src, "_supports") else True
            try:
                evs = src.fetch_for_vegas(vegas_sid)
            except Exception as e:
                errors.append(f"{src.name}: {e}")
                if self.strict and primary is None and supports:
                    # az elsődleges forrás hasalt el -> nincs fallback
                    self.last_errors = errors
                    raise ReferenceDown(f"{src.name}: {e}") from e
                continue
            if evs is None:      # ez a forrás nem ismeri ezt a sportot
                continue
            any_supported = True
            if primary is None:
                primary = src
                for ev in evs:
                    combined[_norm(ev.home) + "|" + _norm(ev.away)] = ev
                if self.strict and not self.fill_gaps:
                    break        # a fair vonal megvan, gyengébb forrás nem hígít
                continue
            for ev in evs:       # csak a HIÁNYZÓ meccseket tölti (fill_gaps)
                combined.setdefault(_norm(ev.home) + "|" + _norm(ev.away), ev)
        self.last_errors = errors
        # ÜRES listára itt NEM dobunk: egy szezonon kívüli sport (nyáron a
        # jégkorong) jogosan üres. Azt, hogy az üresség baj-e, a hívó dönti el,
        # mert csak ő látja, hogy a vegas oldalon van-e egyáltalán meccs.
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
