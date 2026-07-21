"""Automata TÉNYLEGES fogadás-megrakó a vegas.hu-ra (Playwright böngésző-vezérlés).

A motor (engine.py) a 'biztos' value tippeket ide sorba állítja; ez a modul egy
állandó Chromium-profillal (egyszeri kézi belépés után a munkamenet megmarad)
a vegas.hu felületén megkeresi a meccset, rákattint a tippre, beírja a tétet
és megerősíti a fogadást.

Biztonsági korlátok:
  - dry_run: minden lépést végigcsinál, de a végső megerősítésre NEM kattint
    (első éles használat előtt ezzel kalibrálunk),
  - tét = Kelly-tét × stake_multiplier (kis tőkénél pl. 2×), tét-sapka a tőke
    %-ában, napi össztét-sapka, odds-eltérés tolerancia a betslipben,
  - kudarc esetén NINCS automatikus újrapróba (a kétszeri megrakás rosszabb,
    mint egy kihagyás) – minden lépésről képernyőkép + sor a logba,
  - ha a megerősítés után nem jön egyértelmű visszaigazolás, a tétet
    'megrakott'-nak könyveljük (óvatos: inkább számoljunk vele, mint duplázzunk).

Egyszeri beállítás:
    pip install playwright
    playwright install chromium
    python -m valuebet.autobet --login    # belépés kézzel; a profil megjegyzi

Kalibrálás (éles rakás nélkül, képernyőképekkel):
    python -m valuebet.autobet --test
"""
import json
import os
import queue
import re
import threading
import time
from datetime import date, datetime

from . import value as V

DEFAULTS = {
    "enabled": False,
    "dry_run": True,
    "bankroll": 5000,
    "stake_multiplier": 2.0,
    "min_stake": 100,
    "stake_round": 10,
    "max_stake_pct": 10.0,
    "max_odds": 2.0,
    "max_daily_stake": 1500,
    "odds_tolerance_pct": 3.0,
    "headless": False,
    "site_url": "https://www.vegas.hu/",
    "sport_url": "https://www.vegas.hu/sports",
    "profile_dir": "pw_profile",
    "log_dir": "autobet_logs",
    "username": "",
    "password": "",
    "confirm_texts": ["Fogadás megtétele", "Fogadás elküldése", "Fogadok",
                      "Megerősítés", "Place bet"],
    "success_texts": ["sikeres", "elfogadva", "azonosító", "szelvényszám"],
    "cookie_texts": ["Nem, köszönöm", "Elfogad", "Összes elfogadása", "Rendben", "Accept"],
}


def _now_tag():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _odds_hu(o):
    """1.85 -> a felületen '1,85' vagy '1.85' formában kereshető regex."""
    return re.compile(rf"\b{o:.2f}\b".replace(".", "[.,]"))


class AutoBetter:
    """Háttérszálas megrakó-sor. A Playwright egyetlen dedikált szálon fut."""

    def __init__(self, cfg, on_placed=None, on_failed=None):
        ab = dict(DEFAULTS)
        ab.update(cfg.get("autobet", {}))
        self.cfg = ab
        self.kelly_fraction = cfg.get("value", {}).get("kelly_fraction", 0.25)
        self.on_placed = on_placed
        self.on_failed = on_failed
        self.max_odds = float(ab["max_odds"])
        self.dry_run = bool(ab["dry_run"])
        self._q = queue.Queue()
        self._stop = threading.Event()
        self._thread = None
        os.makedirs(ab["log_dir"], exist_ok=True)
        self._daily_path = os.path.join(ab["log_dir"], "daily.json")

    # ---------- tét-méretezés ----------
    def stake(self, fair_p, odds, open_stake=0.0, settled_pnl=0.0):
        """Tét Ft-ban a KIS tőkére méretezve: Kelly × kelly_fraction × multiplier.

        A rendelkezésre álló tőke = bankroll + lezárt valós P/L − nyitott tétek.
        0-t ad, ha nincs edge, nincs elég tőke, vagy betelt a napi sapka."""
        ab = self.cfg
        bankroll = float(ab["bankroll"]) + settled_pnl
        avail = bankroll - open_stake
        full = V.kelly_fraction(fair_p, odds) * self.kelly_fraction
        if full <= 0 or avail <= 0:
            return 0
        raw = bankroll * full * float(ab["stake_multiplier"])
        if ab["max_stake_pct"] > 0:
            raw = min(raw, bankroll * float(ab["max_stake_pct"]) / 100.0)
        step = int(ab["stake_round"]) or 10
        st = int(round(raw / step) * step)
        if st < int(ab["min_stake"]):
            st = int(ab["min_stake"])
        if st > avail:
            return 0
        if self._staked_today() + st > float(ab["max_daily_stake"]):
            return 0
        return st

    def _staked_today(self):
        try:
            with open(self._daily_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            return float(d.get(date.today().isoformat(), 0))
        except Exception:
            return 0.0

    def _add_staked_today(self, amount):
        try:
            d = {}
            if os.path.exists(self._daily_path):
                with open(self._daily_path, "r", encoding="utf-8") as f:
                    d = json.load(f)
            key = date.today().isoformat()
            d = {key: float(d.get(key, 0)) + amount}   # csak a mai napot tartjuk
            with open(self._daily_path, "w", encoding="utf-8") as f:
                json.dump(d, f)
        except Exception:
            pass

    # ---------- sor + szál ----------
    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._q.put(None)

    def enqueue(self, payload):
        """A motor hívja: egy megrakandó tipp (paper_payload + 'stake')."""
        self._q.put(payload)

    def _worker(self):
        while not self._stop.is_set():
            job = self._q.get()
            if job is None:
                break
            try:
                status = self._process(job)
                if status in ("placed", "unverified"):
                    self._add_staked_today(job.get("stake", 0))
                    if self.on_placed:
                        job["autobet_status"] = status
                        self.on_placed(job)
                self._log({"ok": True, "status": status, "event": job.get("event"),
                           "tip": job.get("tip"), "stake": job.get("stake")})
            except Exception as e:
                self._log({"ok": False, "error": str(e), "event": job.get("event"),
                           "tip": job.get("tip"), "stake": job.get("stake")})
                if self.on_failed:
                    self.on_failed(job, str(e))

    # ---------- naplózás ----------
    def _log(self, data):
        data["ts"] = datetime.now().isoformat(timespec="seconds")
        line = json.dumps(data, ensure_ascii=False)
        print(f"[autobet] {line}")
        try:
            with open(os.path.join(self.cfg["log_dir"], "autobet.jsonl"),
                      "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def _shot(self, page, step):
        try:
            path = os.path.join(self.cfg["log_dir"], f"{_now_tag()}_{step}.png")
            page.screenshot(path=path, full_page=False)
        except Exception:
            pass

    # ---------- Playwright ----------
    def _process(self, job, force_dry=False):
        """Egy fogadás megrakása. Visszatérés: 'dry' | 'placed' | 'unverified'."""
        from playwright.sync_api import sync_playwright
        ab = self.cfg
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                ab["profile_dir"], headless=bool(ab["headless"]),
                viewport={"width": 1400, "height": 900}, locale="hu-HU")
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                self._open_event(page, job)
                fr = self._widget(page)
                self._click_odds(fr, page, job)
                return self._fill_and_confirm(fr, page, job,
                                              dry=force_dry or self.dry_run)
            finally:
                ctx.close()

    def _widget(self, page):
        """Az Altenar sportsbook sokszor iframe-ben él – megkeressük a keretét."""
        for f in page.frames:
            u = (f.url or "").lower()
            if "biahosted" in u or "altenar" in u:
                return f
        return page.main_frame

    def _dismiss_cookies(self, page):
        """Minden ismert felugrót bezár (süti-sáv, push-értesítés kérdés stb.)."""
        for t in self.cfg["cookie_texts"]:
            try:
                page.get_by_role("button", name=re.compile(t, re.I)).first.click(timeout=1500)
                page.wait_for_timeout(800)
            except Exception:
                continue

    def _click(self, page, locator, what):
        """Kattintás felugró-védelemmel: a push-értesítés ablak KÉSLELTETVE is
        felugorhat és elfogja a kattintást – ilyenkor bezárjuk és újrapróbáljuk."""
        for attempt in (1, 2, 3):
            try:
                locator.click(timeout=5000)
                return
            except Exception as e:
                if attempt == 3:
                    raise RuntimeError(f"{what}: {e}")
                self._dismiss_cookies(page)
                page.wait_for_timeout(500)

    def _ensure_logged_in(self, page):
        """Ha a fejlécben 'Belépés' gomb látszik, bejelentkezik a config-beli
        adatokkal (a vegas.hu a böngésző bezárásakor/inaktivitásnál kiléptet,
        ezért minden megrakás előtt ellenőrizni kell)."""
        ab = self.cfg
        login_re = re.compile(r"^\s*belépés\s*$", re.I)
        btn = None
        for get in (lambda: page.get_by_role("button", name=login_re).first,
                    lambda: page.get_by_role("link", name=login_re).first,
                    lambda: page.get_by_text(login_re).first):
            try:
                cand = get()
                cand.wait_for(state="visible", timeout=2500)
                btn = cand
                break
            except Exception:
                continue
        if btn is None:
            return  # nincs Belépés gomb -> már be vagyunk lépve

        user, pwd = str(ab.get("username", "")), str(ab.get("password", ""))
        if not user or not pwd:
            raise RuntimeError("nincs belépve, és nincs autobet.username/password "
                               "a config.json-ban")
        btn.click(timeout=5000)
        page.wait_for_timeout(2500)
        self._shot(page, "00_login_urlap")

        ufield = None
        for sel in ["input[type='email']", "input[name*='email' i]",
                    "input[name*='user' i]", "input[placeholder*='mail' i]",
                    "input[placeholder*='elhasznál' i]", "input[type='text']"]:
            try:
                cand = page.locator(sel).first
                cand.wait_for(state="visible", timeout=2500)
                ufield = cand
                break
            except Exception:
                continue
        pfield = page.locator("input[type='password']").first
        if ufield is None:
            raise RuntimeError("nem találom a belépési űrlap felhasználó-mezőjét")
        ufield.fill(user)
        pfield.fill(pwd)
        submitted = False
        for t in ("Belépés", "Bejelentkezés", "Login"):
            try:
                page.get_by_role("button", name=re.compile(t, re.I)).last.click(timeout=2500)
                submitted = True
                break
            except Exception:
                continue
        if not submitted:
            pfield.press("Enter")
        page.wait_for_timeout(7000)
        self._shot(page, "00_login_utan")
        try:
            still = page.get_by_role("button", name=login_re).first
            if still.is_visible():
                raise RuntimeError("a belépés nem sikerült (a 'Belépés' gomb "
                                   "továbbra is látszik) – ellenőrizd a jelszót")
        except RuntimeError:
            raise
        except Exception:
            pass  # a gomb eltűnt -> rendben

    def _open_event(self, page, job):
        """Sport-oldal -> belépés-ellenőrzés -> keresés -> meccs megnyitása."""
        page.goto(self.cfg["sport_url"], wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(5000)
        self._dismiss_cookies(page)
        self._ensure_logged_in(page)
        self._shot(page, "01_sport_oldal")

        home, _, away = job.get("event", "").partition(" - ")
        fr = self._widget(page)

        # kereső: a vegas.hu/sports bal oldali 'Csapat vagy bajnokság' mezője
        # (a sportsbook lassan tölt be, ezért itt türelmesen várunk rá)
        box = None
        for sel in ["input[placeholder*='sapat' i]", "input[placeholder*='ajnoks' i]",
                    "input[type='search']", "input[placeholder*='eres' i]",
                    "[class*='search' i] input"]:
            try:
                cand = fr.locator(sel).first
                cand.wait_for(state="visible", timeout=30000 if box is None else 3000)
                box = cand
                break
            except Exception:
                continue
        if box is None:
            raise RuntimeError("nem találom a keresőmezőt a sport-oldalon "
                               "(betöltött egyáltalán a sportsbook?)")
        box.fill(home)
        page.wait_for_timeout(2500)
        self._shot(page, "02_kereses")

        # találat: olyan sor, amiben a hazai (és lehetőleg a vendég) név is szerepel
        hit = None
        try:
            hit = fr.get_by_text(re.compile(re.escape(home), re.I)).filter(
                has_text=re.compile(re.escape(away or ""), re.I)).first
            hit.wait_for(state="visible", timeout=4000)
        except Exception:
            hit = fr.get_by_text(re.compile(re.escape(home), re.I)).first
        self._click(page, hit, "keresési találat")
        page.wait_for_timeout(3500)
        self._shot(page, "03_meccs_oldal")

    # a piac-fejléc szövege, amin belül a tippet keressük
    _MARKET_HINTS = {
        "ml": re.compile(r"meccsgyőztes|1x2|győztes|match winner", re.I),
        "ou": re.compile(r"száma|over/under|total", re.I),
        "ah": re.compile(r"hendikep|handicap", re.I),
    }

    def _selection_re(self, job):
        """A kiválasztás (kimenetel) felirata a meccs-oldalon, regexként."""
        market = job.get("market", "ml")
        tip = str(job.get("tip", ""))
        if market == "ml":
            if tip.upper().startswith("X"):
                return re.compile("döntetlen", re.I)
            name = re.sub(r"^[12]\s*—\s*", "", tip).strip()
            return re.compile(re.escape(name), re.I)
        if market == "ou":
            side = (r"felett|több|over" if tip.lower().startswith("több")
                    else r"alatt|kevesebb|under")
            return re.compile(side, re.I)
        # hendikep: 'Hendikep Csapat (+1.5)' -> a csapatnév
        name = re.sub(r"^Hendikep\s*", "", tip)
        name = re.sub(r"\(.*?\)", "", name).strip()
        return re.compile(re.escape(name), re.I)

    def _slip_has_pick(self, fr, odds_txt):
        """Igaz, ha a jobb oldali szelvényen már van kiválasztott kimenetel."""
        try:
            t = fr.get_by_text(re.compile("szelvény", re.I)).last.locator(
                "xpath=ancestor::*[3]").inner_text(timeout=2000)
        except Exception:
            try:
                t = fr.locator("body").inner_text(timeout=2000)
            except Exception:
                return False
        return "Válassz egy kimenetelt" not in t

    def _click_odds(self, fr, page, job):
        """Az odds-cella megkeresése és megnyomása, majd ELLENŐRZÉS a szelvényen.

        A vegas.hu-n a kimenetel egy cella: [kiválasztás neve ... odds]. A pontos
        odds-feliratú elemet keressük (pl. '1,33'), és azt fogadjuk el, amelyik
        cellájában (szülő-elem, rövid szöveg) a kiválasztás neve is szerepel."""
        want = float(job["odds"])
        odds_txt = f"{want:.2f}".replace(".", ",")
        exact = re.compile(rf"^\s*{re.escape(odds_txt)}\s*$")
        sel_re = self._selection_re(job)

        cands = fr.get_by_text(exact)
        try:
            n = cands.count()
        except Exception:
            n = 0
        target = None
        for i in range(min(n, 25)):
            el = cands.nth(i)
            try:
                cell_txt = el.locator("xpath=..").inner_text(timeout=1200)
            except Exception:
                continue
            # a cella rövid (név + odds) – a hosszú találat egy nagy konténer
            if len(cell_txt) <= 120 and sel_re.search(cell_txt):
                target = el
                break
        if target is None and n:
            # tartalék: nagyszülő-szinten is megnézzük (más DOM-tagolás esetén)
            for i in range(min(n, 25)):
                el = cands.nth(i)
                try:
                    cell_txt = el.locator("xpath=../..").inner_text(timeout=1200)
                except Exception:
                    continue
                if len(cell_txt) <= 160 and sel_re.search(cell_txt):
                    target = el
                    break
        if target is None:
            raise RuntimeError(
                f"nem találom az odds-cellát ({odds_txt} + {sel_re.pattern}) a meccs-oldalon "
                f"– lehet, hogy az odds időközben átárazódott")
        self._click(page, target, "odds-cella")
        page.wait_for_timeout(2500)
        self._shot(page, "04_odds_kattintva")
        if not self._slip_has_pick(fr, odds_txt):
            raise RuntimeError("az odds-kattintás után a szelvény üres maradt "
                               "(nem került fel a kiválasztás)")

    def _fill_and_confirm(self, fr, page, job, dry):
        """Betslip: tét beírása, odds-ellenőrzés, megerősítés (vagy dry-run leállás)."""
        ab = self.cfg
        stake = int(job["stake"])

        slip = None
        for sel in ["[class*='betslip' i]", "[class*='BetSlip']",
                    "[data-testid*='slip' i]", "[class*='coupon' i]"]:
            try:
                cand = fr.locator(sel).first
                cand.wait_for(state="visible", timeout=3000)
                slip = cand
                break
            except Exception:
                continue
        if slip is None:
            # tartalék: a jobb oldali 'Szelvény' feliratú panel legszűkebb konténere
            try:
                cand = fr.locator("div", has=fr.get_by_text(
                    re.compile(r"^\s*Szelvény", re.I))).filter(
                    has=fr.locator("input")).last
                cand.wait_for(state="visible", timeout=3000)
                slip = cand
            except Exception:
                pass
        scope = slip if slip is not None else fr

        box = None
        for sel in ["input[inputmode='decimal']", "input[type='number']",
                    "input[placeholder*='ét' i]", "input[type='text']"]:
            try:
                cand = (scope.locator(sel) if slip is not None
                        else fr.locator(sel)).first
                cand.wait_for(state="visible", timeout=3000)
                box = cand
                break
            except Exception:
                continue
        if box is None:
            raise RuntimeError("nem találom a tét-beviteli mezőt a betslipben")

        # odds-ellenőrzés: a slipben látható odds ne legyen érdemben rosszabb
        tol = float(ab["odds_tolerance_pct"]) / 100.0
        want = float(job["odds"])
        try:
            slip_text = (slip.inner_text(timeout=2000) if slip is not None
                         else fr.locator("body").inner_text(timeout=2000))
            nums = [float(x.replace(",", ".")) for x in
                    re.findall(r"\b\d{1,2}[.,]\d{2}\b", slip_text)]
            near = [x for x in nums if abs(x - want) / want <= max(tol, 0.10)]
            if near and min(near, key=lambda x: abs(x - want)) < want * (1 - tol):
                raise RuntimeError(
                    f"az odds elmozdult ({min(near, key=lambda x: abs(x - want))} < "
                    f"{want} -{ab['odds_tolerance_pct']}%), kihagyva")
        except RuntimeError:
            raise
        except Exception:
            pass  # ha nem olvasható ki, nem blokkolunk – a képernyőkép megmarad

        box.fill(str(stake))
        page.wait_for_timeout(1200)
        self._shot(page, "05_tet_beirva")

        if dry:
            # próba-üzem: NEM erősítünk meg, a szelvényt kiürítjük
            try:
                fr.get_by_text(re.compile("Szelvény törlése", re.I)).first.click(timeout=2000)
            except Exception:
                for sel in ["[aria-label*='öröl' i]", "[class*='remove' i]",
                            "[class*='trash' i]", "[class*='delete' i]"]:
                    try:
                        scope.locator(sel).first.click(timeout=1500)
                        break
                    except Exception:
                        continue
            return "dry"

        clicked = False
        for t in ab["confirm_texts"]:
            try:
                self._click(page, fr.get_by_role("button", name=re.compile(t, re.I)).first,
                            "megerősítés-gomb")
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            raise RuntimeError("nem találom a 'Fogadás megtétele' gombot")
        page.wait_for_timeout(4000)
        self._shot(page, "06_megerositve")

        body = ""
        try:
            body = fr.locator("body").inner_text(timeout=3000)
        except Exception:
            pass
        ok = any(t.lower() in body.lower() for t in ab["success_texts"])
        return "placed" if ok else "unverified"


# ---------- parancssor: --login és --test ----------
def _load_cfg(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _cmd_login(cfg):
    """Egyszeri kézi belépés; az állandó profil megjegyzi a munkamenetet.

    Interaktív konzolból ENTER zárja; ha nincs konzol (pl. háttérből indítva),
    elég BEZÁRNI a böngészőablakot, amikor a belépés kész."""
    import sys
    from playwright.sync_api import sync_playwright
    ab = dict(DEFAULTS)
    ab.update(cfg.get("autobet", {}))
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            ab["profile_dir"], headless=False,
            viewport={"width": 1400, "height": 900}, locale="hu-HU")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        # best-effort betöltés: lassú oldalnál se zárjuk be a böngészőt,
        # legfeljebb kézzel kell beírni a címet
        try:
            page.goto(ab["site_url"], wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            print(f"(A kezdőlap betöltése akadozott: {e} – a böngésző nyitva marad, "
                  f"írd be kézzel: {ab['site_url']})")
        print("\nLépj be a vegas.hu fiókodba a megnyílt böngészőben.")
        # interaktív konzolból ENTER zárja; ha nincs valódi stdin (háttérből
        # indítva az input() EOF-ot kap), a böngészőablak BEZÁRÁSA fejezi be
        done = False
        try:
            if sys.stdin and sys.stdin.isatty():
                input("Ha kész (látod, hogy be vagy lépve), nyomj ENTER-t itt... ")
                done = True
        except (EOFError, OSError):
            pass
        if not done:
            print("Ha kész vagy, egyszerűen ZÁRD BE a böngészőablakot.")
            try:
                ctx.wait_for_event("close", timeout=0)
            except Exception:
                pass
        try:
            ctx.close()
        except Exception:
            pass
    print("Kész – a belépés elmentve a profilba. Az autobet mostantól tudja használni.")


def _cmd_test(cfg):
    """Egy kör a value-motorral, majd az első biztos tippen DRY-RUN próba-megrakás."""
    from .http import Http
    from .engine import ValueEngine
    hcfg = cfg.get("http", {})
    http = Http(verify_ssl=hcfg.get("verify_ssl", True),
                delay_sec=hcfg.get("request_delay_sec", 0.3))
    print("Value-keresés (egy kör)...")
    engine = ValueEngine(http, cfg)
    engine._cycle()
    ab = AutoBetter(cfg)
    now = time.time()
    pick = None
    fallback = None
    with engine._lock:
        for rec in engine._bets.values():
            if engine._is_solid(rec, now) and rec.get("odds", 99) <= ab.max_odds:
                pick = dict(rec)
                break
            # tartalék a teszthez: a LEGKISEBB oddsú tipp (mainstream meccs,
            # biztosan kereshető) – élesben ilyet úgysem rakna meg a motor
            if fallback is None or rec.get("odds", 99) < fallback.get("odds", 99):
                fallback = dict(rec)
    pick = pick or fallback
    if not pick:
        print("Most nincs value bet – próbáld később.")
        return
    stake = ab.stake(pick["fair_p"], pick["odds"]) or int(ab.cfg["min_stake"])
    job = {"event": pick.get("event"), "market": pick.get("market"),
           "tip": pick.get("tip"), "odds": pick.get("odds"), "stake": stake}
    print(f"Próba (DRY-RUN): {job['event']} | {job['tip']} @ {job['odds']} | tét {stake} Ft")
    status = ab._process(job, force_dry=True)
    print(f"Eredmény: {status}. Képernyőképek: {ab.cfg['log_dir']}\\")


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(prog="valuebet.autobet",
                                description="vegas.hu automata megrakó (login/teszt)")
    p.add_argument("--config", default="config.json")
    p.add_argument("--login", action="store_true", help="egyszeri kézi belépés a profilba")
    p.add_argument("--test", action="store_true", help="dry-run próba az aktuális value beten")
    args = p.parse_args(argv)
    cfg = _load_cfg(args.config)
    if args.login:
        _cmd_login(cfg)
    elif args.test:
        _cmd_test(cfg)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
