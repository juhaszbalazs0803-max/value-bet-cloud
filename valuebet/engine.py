"""Élő value-motor: háttérszálon pollozza a vegas.hu-t és a Pinnacle-t,
párosít, value-t számol minden piacon, és karbantart egy élő állapotot.

Funkciók:
  - több piac: meccsgyőztes, Over/Under, hendikep,
  - tőke (bankroll) alapú javasolt tét (Kelly), minimum 100 Ft,
  - megrakott fogadások mentése, lezárása (nyert/vesztett), egyenleg-görbe.
"""
import json
import math
import os
import threading
import time
from datetime import datetime, date

from .vegas import VegasClient, SPORT_NAMES
from .pinnacle import PinnacleClient, SPORT_MAP
from .notify import EmailNotifier
from .telegram import TelegramNotifier, format_tip, BUTTONS
from .sportsdb import SportsDBClient
from .tennisexplorer import TennisExplorerClient
from . import matching, compute, results, bettoken, value as V

STORE_MIN_VALUE = 0.5  # ennél kisebb value-t nem tárolunk
DELETED_TTL = 24 * 3600  # egy törölt fogadást ennyi ideig nem rakunk vissza Telegram-koppintásra


def _round_stake(x, step=100):
    return int(round(x / step) * step)


def _iso_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Settings:
    def __init__(self, cfg):
        v = cfg.get("value", {})
        live = cfg.get("live", {})
        self.min_value_pct = v.get("min_value_pct", 3.0)
        self.min_odds = v.get("min_odds", 1.2)
        self.max_odds = v.get("max_odds", 15.0)
        self.kelly_fraction = v.get("kelly_fraction", 0.25)
        self.sports = list(live.get("sports", cfg.get("vegas", {}).get("sport_ids", [66])))
        self.markets = list(live.get("markets", ["ml", "ou", "ah"]))
        self.bankroll = float(live.get("bankroll", 0))
        self.min_bet = float(live.get("min_bet", 100))
        # egységes (fix) tét az értékeléshez: a balance-görbe és a hozam ezzel számol,
        # hogy a változó tét ne torzítsa a teljesítmény-mérést
        self.unit_stake = float(live.get("unit_stake", 0) or live.get("min_bet", 100))
        # max tét-sapka: egy fogadásra a tőke ennyi %-a (0=nincs sapka). Megvédi a
        # kis tőkét attól, hogy a Kelly egyetlen meccsre túl nagy tétet rakjon.
        self.max_stake_pct = float(live.get("max_stake_pct", 5.0))
        self.only_solid = bool(live.get("only_solid", True))
        self.notify_enabled = bool(cfg.get("notify", {}).get("enabled", False))
        self.telegram_enabled = bool(cfg.get("telegram", {}).get("enabled", False))
        # --- AUTO PAPÍR-FOGADÁS ---
        # A biztos value tippeket nem értesítésként küldjük, hanem a szoftver
        # "megrakja" (papíron) és statisztikát vezet. Csak a max_odds alatti
        # (alapból <=2.00) tippeket rakjuk meg.
        paper = cfg.get("paper", {})
        self.paper_enabled = bool(paper.get("enabled", True))
        self.paper_max_odds = float(paper.get("max_odds", 2.0))
        # --- NAPI JELENTÉS ---
        report = cfg.get("report", {})
        self.report_enabled = bool(report.get("enabled", True))
        self.report_hour = int(report.get("hour", 9))
        solid = live.get("solid", {})
        self.solid_min_limit = float(solid.get("min_limit", 0))
        self.solid_min_age = float(solid.get("min_age_sec", 12))
        self.solid_max_hours = float(solid.get("max_hours_to_start", 0))

    def to_dict(self):
        return {k: getattr(self, k) for k in
                ("min_value_pct", "min_odds", "max_odds", "kelly_fraction",
                 "sports", "markets", "bankroll", "min_bet", "unit_stake", "only_solid",
                 "solid_min_limit", "solid_min_age", "solid_max_hours", "notify_enabled",
                 "telegram_enabled", "paper_enabled", "paper_max_odds",
                 "report_enabled", "report_hour", "max_stake_pct")}

    def update(self, data):
        for k in ("min_value_pct", "min_odds", "max_odds", "kelly_fraction",
                  "bankroll", "min_bet", "unit_stake", "solid_min_limit", "solid_min_age",
                  "solid_max_hours", "paper_max_odds", "max_stake_pct"):
            if data.get(k) is not None:
                setattr(self, k, float(data[k]))
        if data.get("report_hour") is not None:
            self.report_hour = int(data["report_hour"])
        if data.get("only_solid") is not None:
            self.only_solid = bool(data["only_solid"])
        if data.get("notify_enabled") is not None:
            self.notify_enabled = bool(data["notify_enabled"])
        if data.get("telegram_enabled") is not None:
            self.telegram_enabled = bool(data["telegram_enabled"])
        if data.get("paper_enabled") is not None:
            self.paper_enabled = bool(data["paper_enabled"])
        if data.get("report_enabled") is not None:
            self.report_enabled = bool(data["report_enabled"])
        if isinstance(data.get("sports"), list):
            self.sports = [int(x) for x in data["sports"]]
        if isinstance(data.get("markets"), list):
            self.markets = [str(x) for x in data["markets"]]


class ValueEngine:
    def __init__(self, http, cfg, data_path="valuebet_data.json"):
        self.cfg = cfg
        self.vegas = VegasClient(http, cfg["vegas"])
        self.pinnacle = PinnacleClient(http)
        self.devig = cfg.get("reference", {}).get("devig_method", "proportional")
        self.settings = Settings(cfg)
        self.max_plausible = cfg.get("value", {}).get("max_plausible_pct", 30.0)
        live = cfg.get("live", {})
        solid = live.get("solid", {})
        self.solid_min_score = solid.get("min_score", 0.8)
        self.solid_max_odds = solid.get("max_odds", 5.0)
        self.solid_max_value = solid.get("max_value_pct", 20.0)
        self.notifier = EmailNotifier(cfg)
        self.telegram = TelegramNotifier(cfg)
        # visszamenőleges végeredmény-források a papír-tételek lezárásához:
        #  - TheSportsDB: foci/kosár/jégkorong + nagy tornás tenisz
        #  - tennisexplorer: TELJES tenisz-mezőny (Challenger/ITF is)
        self.sportsdb = SportsDBClient(http, cfg)
        self.tennisexplorer = TennisExplorerClient(http, cfg)
        self.retroactive = cfg.get("results", {}).get("retroactive", True)
        self.notify_min = cfg.get("notify", {}).get("min_value_pct", 3.0)
        # elküldött tippek (kulcs = sport:meccsId:tipp) -> első értesítés ideje.
        # Tartós (mentődik), hogy UGYANAZT a tippet ne küldjük el kétszer, akkor se,
        # ha a meccs eltűnt/visszajött a listából, vagy újraindult az app.
        self._notified = set()
        self._notified_ts = {}
        # auto-megrakott (papír) tételek TARTALOM-kulcsai -> első megrakás ideje.
        # Tartalom-alapú (csapat+tipp+nap), hogy egy eltűnő/visszatérő meccs (új
        # vegas event-id) ne kerüljön be kétszer.
        self._papered = {}
        # napi jelentés: melyik napon küldtünk utoljára (ne menjen naponta többször)
        self._last_report_date = None
        self.notify_keep_days = cfg.get("notify", {}).get("keep_days", 7)
        self._notify_armed_at = time.time() + cfg.get("notify", {}).get("arm_after_sec", 30)
        self.poll_interval = live.get("poll_interval_sec", 5)
        self.grace_sec = live.get("grace_sec", 45)
        self.match_cfg = cfg.get("matching", {})
        self.data_path = data_path
        # email-válaszok beolvasása + automatikus lezárás
        self.inbox_enabled = cfg.get("inbox", {}).get("enabled", True)
        self.inbox_poll_sec = cfg.get("inbox", {}).get("poll_sec", 120)
        self.settle_poll_sec = cfg.get("results", {}).get("settle_poll_sec", 30)
        # Telegram gomb-válaszok beolvasása (getUpdates)
        self.tg_poll_sec = cfg.get("telegram", {}).get("poll_sec", 30)
        self._last_inbox = 0.0
        self._last_settle = 0.0
        self._tg_offset = None
        self._tg_thread_running = False
        self.last_inbox_result = None
        self.last_tg_result = None

        self._lock = threading.RLock()
        self._bets = {}
        self._placed = []
        self._deleted = {}   # törölt fogadások kulcsai -> törlés ideje (resurrection-védelem)
        self._next_id = 1
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.meta = {"last_cycle": None, "last_ok": None, "cycle_ms": 0,
                     "vegas_events": 0, "pinn_events": 0, "matched": 0,
                     "errors": [], "running": False}
        self._load()

    # ---------- perzisztencia ----------
    def _load(self):
        if not os.path.exists(self.data_path):
            return
        try:
            with open(self.data_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if "settings" in d:
                self.settings.update(d["settings"])
            self._placed = d.get("placed", [])
            self._next_id = max([b["id"] for b in self._placed], default=0) + 1
            nd = d.get("notified", {})
            if isinstance(nd, dict):
                self._notified_ts = {k: float(v) for k, v in nd.items()}
                self._notified = set(self._notified_ts)
            if d.get("tg_offset") is not None:
                self._tg_offset = int(d["tg_offset"])
            dd = d.get("deleted", {})
            if isinstance(dd, dict):
                self._deleted = {k: float(v) for k, v in dd.items()}
            pp = d.get("papered", {})
            if isinstance(pp, dict):
                self._papered = {k: float(v) for k, v in pp.items()}
            if d.get("last_report_date"):
                self._last_report_date = str(d["last_report_date"])
        except Exception:
            pass

    def _save(self):
        try:
            with open(self.data_path, "w", encoding="utf-8") as f:
                json.dump({"settings": self.settings.to_dict(), "placed": self._placed,
                           "notified": self._notified_ts, "tg_offset": self._tg_offset,
                           "deleted": self._deleted, "papered": self._papered,
                           "last_report_date": self._last_report_date},
                          f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---------- háttérszál ----------
    def start(self):
        self.meta["running"] = True
        threading.Thread(target=self._loop, daemon=True).start()
        # külön szál a Telegram gomb-válaszokra (long-poll = AZONNALI reakció,
        # nem kell a fő ciklusra várni → nincs lassú „pörgés" a gomb után)
        if self.telegram.configured():
            threading.Thread(target=self._telegram_loop, daemon=True).start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def refresh(self):
        """Azonnali poll-ciklus kérése (Frissítés gomb)."""
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self._cycle()
                self.meta["last_ok"] = time.time()
            except Exception as e:
                self.meta["errors"] = [str(e)]
            now = time.time()
            if now - self._last_settle >= self.settle_poll_sec:
                self._last_settle = now
                try:
                    self._settle_pass(now)
                except Exception as e:
                    self.meta["errors"] = [f"lezárás: {e}"]
            if self.inbox_enabled and now - self._last_inbox >= self.inbox_poll_sec:
                self._last_inbox = now
                try:
                    self.check_inbox()
                except Exception as e:
                    self.last_inbox_result = {"ok": False, "reason": str(e)}
            try:
                self._maybe_daily_report(now)
            except Exception as e:
                self.meta["errors"] = [f"napi jelentés: {e}"]
            self.meta["last_cycle"] = time.time()
            self.meta["cycle_ms"] = int((time.time() - t0) * 1000)
            self._wake.wait(self.poll_interval)
            self._wake.clear()

    def _cycle(self):
        with self._lock:
            sports = list(self.settings.sports)
        max_diff = self.match_cfg.get("max_start_diff_minutes", 90)
        min_score = self.match_cfg.get("min_token_score", 0.6)
        now = time.time()
        seen = set()
        veg_total = pinn_total = matched_total = 0

        for sid in sports:
            pinn_sid = SPORT_MAP.get(sid)
            if not pinn_sid:
                continue
            vegas_events = self.vegas.fetch_sport(sid)
            ref_events = self.pinnacle.fetch_sport(pinn_sid)
            veg_total += len(vegas_events)
            pinn_total += len(ref_events)
            pairs = matching.match_events(vegas_events, ref_events, max_diff, min_score)
            matched_total += len(pairs)

            for ve, re_, swapped, score in pairs:
                for b in compute.compute_bets(ve, re_, swapped, self.devig):
                    val = b["value_pct"]
                    if val < STORE_MIN_VALUE or val > self.max_plausible:
                        continue
                    key = f"{sid}:{ve.id}:{b['subkey']}"
                    seen.add(key)
                    with self._lock:
                        rec = self._bets.get(key)
                        if rec is None:
                            rec = {"key": key, "first_seen": now, "stable_since": now}
                        elif not rec.get("valid", False):
                            # volt érvénytelen/eltűnt -> a stabilitás újraindul
                            rec["stable_since"] = now
                        rec.update({
                            "sport_id": sid, "sport": SPORT_NAMES.get(sid, str(sid)),
                            "event": f"{ve.home} - {ve.away}",
                            "start": ve.start.isoformat() if ve.start else None,
                            "start_ts": ve.start.timestamp() if ve.start else None,
                            "market": b["market"], "market_name": b["market_name"],
                            "tip": b["tip"], "odds": b["odds"], "ref_odds": b["ref_odds"],
                            "fair_odds": b["fair_odds"], "fair_pct": b["fair_pct"],
                            "fair_p": b["fair_p"], "value_pct": val, "league": re_.league,
                            "pinn_url": b.get("pinn_url"), "match_score": score,
                            "limit": b.get("limit", 0), "last_seen": now, "valid": True,
                        })
                        self._bets[key] = rec

        with self._lock:
            for key, rec in list(self._bets.items()):
                if key not in seen:
                    rec["valid"] = False
                if now - rec["last_seen"] > self.grace_sec:
                    del self._bets[key]
                    # FONTOS: a _notified-ből NEM töröljük, hogy ugyanaz a tipp
                    # később (visszatérő meccs) ne menjen ki újra.
            self.meta.update({"vegas_events": veg_total, "pinn_events": pinn_total,
                              "matched": matched_total, "errors": []})

        self._capture_clv(now)
        self._auto_paper(now)
        self._maybe_notify(now)

    # ---------- CLV (záró-odds) követés ----------
    def _capture_clv(self, now):
        """A megrakott (nyitott) tétekhez a kezdésig FRISSÜLŐ Pinnacle záró-oddsot
        rögzíti. Amíg a meccs el nem kezdődött, minden körben felülírja a legutóbbi
        Pinnacle (vig nélküli) oddszal – így kezdéskor a záró-vonalat tartja. A CLV
        azt méri, hogy a megfogott odds verte-e az éles iroda záró (valós) oddsát:
        clv% = (fogadott_odds / pinnacle_záró_fair_odds − 1) × 100. Pozitív = jó jel,
        hosszú távon ez a value bizonyítéka (megbízhatóbb, mint a rövidtávú yield)."""
        with self._lock:
            changed = False
            for b in self._placed:
                if b.get("status") != "pending" or not b.get("key"):
                    continue
                rec = self._bets.get(b["key"])
                st = b.get("start_ts") or _iso_ts(b.get("start"))
                pre_kickoff = st is None or now < st
                if rec and rec.get("valid") and rec.get("fair_odds") and pre_kickoff:
                    b["close_ref_odds"] = rec["ref_odds"]
                    b["close_fair_odds"] = rec["fair_odds"]
                    b["close_fair_p"] = rec["fair_p"]
                    b["close_ts"] = now
                    changed = True
                cf = b.get("close_fair_odds")
                if cf and b.get("odds"):
                    clv = round((b["odds"] / cf - 1) * 100, 2)
                    if b.get("clv_pct") != clv:
                        b["clv_pct"] = clv
                        changed = True
            if changed:
                self._save()

    def _is_solid(self, rec, now):
        s = self.settings
        stable = now - rec.get("stable_since", rec["last_seen"])
        hts = (rec["start_ts"] - now) / 3600.0 if rec.get("start_ts") else None
        within = s.solid_max_hours <= 0 or (hts is not None and 0 <= hts <= s.solid_max_hours)
        return (rec.get("valid")
                and rec.get("match_score", 0) >= self.solid_min_score
                and rec["odds"] <= self.solid_max_odds
                and rec["value_pct"] <= self.solid_max_value
                and stable >= s.solid_min_age
                and rec.get("limit", 0) >= s.solid_min_limit
                and within)

    def _maybe_notify(self, now):
        """Új, biztos value tippekről email/Telegram (a felfutási idő alatt csak előjegyzés)."""
        email_on = self.settings.notify_enabled and self.notifier.configured()
        tg_on = self.settings.telegram_enabled and self.telegram.configured()
        if not (email_on or tg_on):
            return
        armed = now >= self._notify_armed_at
        fresh = []
        changed = False
        with self._lock:
            for key, rec in self._bets.items():
                if (self._is_solid(rec, now) and rec["value_pct"] >= self.notify_min
                        and key not in self._notified):
                    self._notified.add(key)
                    self._notified_ts[key] = now
                    changed = True
                    if armed:
                        fresh.append(dict(rec))
            # régi bejegyzések takarítása (a lista ne nőjön korlátlanul)
            cutoff = now - self.notify_keep_days * 86400
            for k in [k for k, t in self._notified_ts.items() if t < cutoff]:
                del self._notified_ts[k]
                self._notified.discard(k)
                changed = True
            if changed:
                self._save()
        if fresh:
            self._send_notification(fresh)

    def _tip_dict(self, rec):
        """A tipp egységes adat-dictje (Telegram-üzenethez + 'Megraktam' tokenhez)."""
        st = self._stake(rec["fair_p"], rec["odds"])
        pct = V.kelly_fraction(rec["fair_p"], rec["odds"]) * self.settings.kelly_fraction * 100
        return {
            "key": rec.get("key"), "sport": rec.get("sport", ""),
            "event": rec.get("event", ""), "market": rec.get("market", ""),
            "market_name": rec.get("market_name", ""), "tip": rec.get("tip", ""),
            "odds": round(float(rec["odds"]), 3), "stake": int(st),
            "stake_pct": round(pct, 1), "value_pct": rec.get("value_pct", 0),
            "fair_pct": rec.get("fair_pct", 0), "start": rec.get("start"),
            "limit": rec.get("limit", 0), "pinn_url": rec.get("pinn_url", ""),
        }

    def _send_notification(self, bets):
        bets.sort(key=lambda r: -r["value_pct"])
        # Telegram: tippenként egy üzenet, ✅ Megraktam / ❌ Kihagytam gombokkal.
        if self.settings.telegram_enabled and self.telegram.configured():
            for b in bets:
                d = self._tip_dict(b)
                self.telegram.send_async(format_tip(d, bettoken.token_block(d)), BUTTONS)
        # Email: összevont szöveges üzenet.
        if self.settings.notify_enabled and self.notifier.configured():
            lines = [f"{len(bets)} új biztos value tipp a vegas.hu-n:\n"]
            for b in bets:
                lines.append(
                    f"• {b['sport']} | {b['event']}\n"
                    f"  {b['market_name']} – {b['tip']}\n"
                    f"  Vegas odds {b['odds']:.2f} | value +{b['value_pct']}% "
                    f"| Pinnacle limit ${b.get('limit', 0)}\n"
                    f"  Ellenőrzés: {b.get('pinn_url', '')}\n")
            subject = f"🟢 {len(bets)} új value tipp (vegas.hu) – legjobb +{bets[0]['value_pct']}%"
            self.notifier.send_async(subject, "\n".join(lines))

    # ---------- automatikus papír-fogadás ----------
    @staticmethod
    def _content_key(rec):
        """Tartalom-alapú kulcs a papír-dedup-hoz: csapat+csapat+tipp+nap.
        Független a változó vegas event-id-tól, így egy eltűnő/visszatérő meccs
        nem kerül be kétszer."""
        ev = rec.get("event", "")
        home, _, away = ev.partition(" - ")
        parts = str(rec.get("key", "")).split(":")
        subkey = ":".join(parts[2:]) if len(parts) >= 3 else rec.get("market", "")
        day = (rec.get("start") or "")[:10]
        return f"{matching.normalize(home)}|{matching.normalize(away)}|{subkey}|{day}"

    def _paper_payload(self, rec):
        """A megrakott (papír) tét adatai a _add_placed számára."""
        return {
            "key": rec.get("key"), "sport": rec.get("sport", ""),
            "event": rec.get("event", ""), "market": rec.get("market", ""),
            "market_name": rec.get("market_name", ""), "tip": rec.get("tip", ""),
            "odds": float(rec["odds"]), "stake": int(self._stake(rec["fair_p"], rec["odds"])),
            "value_pct": rec.get("value_pct", 0), "fair_pct": rec.get("fair_pct", 0),
            "start": rec.get("start"),
        }

    def _auto_paper(self, now):
        """A biztos value tippeket a szoftver automatikusan 'megrakja' (papíron),
        ha az odds <= paper_max_odds (alapból 2.00). Nem küld értesítést – csak
        statisztikát vezet. Tartalom-alapú dedup, hogy ne kerüljön be kétszer."""
        if not self.settings.paper_enabled:
            return
        max_odds = self.settings.paper_max_odds
        placed_now = 0
        with self._lock:
            for rec in list(self._bets.values()):
                if not self._is_solid(rec, now):
                    continue
                if rec.get("odds", 99) > max_odds:
                    continue
                ck = self._content_key(rec)
                if ck in self._papered:
                    continue
                self._papered[ck] = now
                self._add_placed(self._paper_payload(rec), source="auto")
                placed_now += 1
            # régi tartalom-kulcsok takarítása (a lista ne nőjön korlátlanul)
            cutoff = now - max(self.notify_keep_days, 30) * 86400
            for k in [k for k, t in self._papered.items() if t < cutoff]:
                del self._papered[k]
            if placed_now:
                self._save()
        if placed_now:
            print(f"[paper] {placed_now} tipp automatikusan megrakva (odds<={max_odds}).")

    # ---------- napi jelentés ----------
    def _maybe_daily_report(self, now):
        """Naponta EGYSZER (report_hour után) Telegram-jelentést küld a
        statisztikáról: mennyit nőtt volna a portfólió %-ban + yield, találati
        arány. A jelentés a LOKÁLIS appból megy (a statisztika itt él), ezért a
        gépnek futnia kell aznap (az autostart bejelentkezéskor elindítja)."""
        if not self.settings.report_enabled or not self.telegram.configured():
            return
        if datetime.now().hour < self.settings.report_hour:
            return
        today = date.today().isoformat()
        if self._last_report_date == today:
            return
        try:
            self.telegram.send(self._daily_report_text())
        except Exception as e:
            print(f"[report] HIBA (újrapróba később): {e}")
            return
        self._last_report_date = today
        with self._lock:
            self._save()
        print(f"[report] napi jelentés elküldve ({today}).")

    def send_report_now(self):
        """Kézi jelentés-küldés (gomb/teszt) – a napi automata küldéstől függetlenül."""
        if not self.telegram.configured():
            return {"ok": False, "reason": "Telegram nincs beállítva"}
        try:
            self.telegram.send(self._daily_report_text())
        except Exception as e:
            return {"ok": False, "reason": str(e)}
        self._last_report_date = date.today().isoformat()
        with self._lock:
            self._save()
        return {"ok": True, "ts": now_iso()}

    def _daily_report_text(self):
        with self._lock:
            s = self._stats()
            bankroll = self.settings.bankroll or 0
            open_n = sum(1 for b in self._placed if b["status"] == "pending")
            # mai (utolsó 24h) lezárások
            day_ago = time.time() - 86400
            todays = [b for b in self._placed
                      if b.get("settled_ts") and b["settled_ts"] >= day_ago
                      and b["status"] in ("won", "lost")]
            today_won = sum(1 for b in todays if b["status"] == "won")
        # portfólió-növekedés %: a TÉNYLEGES (Kelly) tétekkel elért P/L a tőkéhez mérve
        growth = (100 * s["real_pnl"] / bankroll) if bankroll else 0
        gsign = "+" if growth >= 0 else ""
        ysign = "+" if s["roi"] >= 0 else ""
        lines = [
            "📊 <b>Napi value-bet jelentés</b>",
            "",
            f"💼 Portfólió: <b>{gsign}{growth:.2f}%</b>  "
            f"({s['real_pnl']:+,} Ft / {bankroll:,.0f} Ft tőke)".replace(",", " "),
            f"📈 Hozam (yield, egys. tét): <b>{ysign}{s['roi']}%</b>",
            f"🎯 Találati arány: <b>{s['hit_rate']}%</b>  "
            f"({s['won']}/{s['settled']} nyert/lezárt)",
            f"🟢 Nyitott (papír) tétel: <b>{open_n}</b>",
            f"🗓️ Utolsó 24h: <b>{len(todays)}</b> lezárt, <b>{today_won}</b> nyert",
        ]
        if s.get("clv_avg") is not None:
            lines.append(f"📐 Átlag CLV: <b>{'+' if s['clv_avg'] >= 0 else ''}{s['clv_avg']}%</b>"
                         f"  ({s['clv_beat_rate']}% verte a zárót)")
        lines.append("")
        lines.append("<i>A szoftver automatikusan 'megrakja' a ≤2.00 oddsú biztos "
                     "value tippeket; valódi fogadás nem történik.</i>")
        return "\n".join(lines)

    # ---------- tét ----------
    def _stake(self, fair_p, odds):
        s = self.settings
        if s.bankroll <= 0:
            return 0
        full = V.kelly_fraction(fair_p, odds) * s.kelly_fraction
        if full <= 0:
            return 0                       # nincs edge -> nincs tét
        raw = s.bankroll * full
        # max tét-sapka: egy fogadásra legfeljebb a tőke max_stake_pct %-a
        if s.max_stake_pct > 0:
            raw = min(raw, s.bankroll * s.max_stake_pct / 100.0)
        st = _round_stake(raw)
        if st < s.min_bet:                 # minden érvényes tipp legalább min_bet
            st = int(s.min_bet)
        return st

    # ---------- megrakott fogadások ----------
    @staticmethod
    def _derive_from_key(key):
        """A snapshot 'key'-jéből (sid:vegasid:subkey) a lezáráshoz kellő mezők."""
        out = {"sport_id": None, "vegas_id": None, "market": "",
               "subkey": "", "selection": "", "line": None}
        parts = str(key or "").split(":")
        if len(parts) >= 3:
            try:
                out["sport_id"] = int(parts[0])
            except ValueError:
                pass
            try:
                out["vegas_id"] = int(parts[1])
            except ValueError:
                pass
            out["subkey"] = ":".join(parts[2:])
            m, sel, line = results.parse_subkey(out["subkey"])
            out["market"], out["selection"], out["line"] = m, sel, line
        return out

    def _add_placed(self, payload, source):
        key = payload.get("key") or ""
        der = self._derive_from_key(key)
        start = payload.get("start")
        rec = {
            "id": self._next_id, "ts": time.time(), "key": key,
            "sport": payload.get("sport", ""),
            "sport_id": der["sport_id"], "vegas_id": der["vegas_id"],
            "market": payload.get("market") or der["market"],
            "subkey": der["subkey"], "selection": der["selection"], "line": der["line"],
            "event": payload.get("event", ""),
            "market_name": payload.get("market_name", ""),
            "tip": payload.get("tip", ""),
            "odds": float(payload.get("odds", 0) or 0),
            "stake": int(payload.get("stake", 0) or 0),
            "value_pct": float(payload.get("value_pct", 0) or 0),
            "fair_pct": float(payload.get("fair_pct", 0) or 0),
            "start": start, "start_ts": _iso_ts(start),
            "status": "pending", "settled_ts": None, "source": source,
            "live_score": None, "live_seen_ts": None,
            "final_score": None, "settle_source": None, "needs_manual": False,
        }
        self._next_id += 1
        self._placed.append(rec)
        self._save()
        return rec

    def place(self, payload):
        # A weben kézzel megrakott fogadás SZÁNDÉKOS – ha korábban töröltük,
        # most engedjük újra (kivesszük a törölt-listából).
        with self._lock:
            key = payload.get("key")
            if key:
                self._deleted.pop(key, None)
            return self._add_placed(payload, source="web")

    def place_from_email(self, bet):
        """A Telegram/email 'Megraktam'-ból elmentett tét (dedup a key-re).

        Ha ezt a fogadást nemrég TÖRÖLTÜK, nem rakjuk vissza – így egy elkóborolt
        vagy ismételt gombnyomás nem támasztja fel a már törölt tételt."""
        key = bet.get("key")
        with self._lock:
            if key and any(b.get("key") == key for b in self._placed):
                return None
            if key and key in self._deleted:
                if time.time() - self._deleted[key] < DELETED_TTL:
                    return None
                del self._deleted[key]   # lejárt – mehet újra
            return self._add_placed(bet, source="email")

    def settle(self, bet_id, result):
        with self._lock:
            for b in self._placed:
                if b["id"] == bet_id:
                    b["status"] = result  # won / lost / void / pending
                    b["settled_ts"] = time.time() if result != "pending" else None
                    self._save()
                    return b
        return None

    def delete(self, bet_id):
        with self._lock:
            now = time.time()
            # a törölt fogadás kulcsát megjegyezzük, hogy egy ismételt Telegram-
            # koppintás ne támassza fel (DELETED_TTL ideig)
            for b in self._placed:
                if b["id"] == bet_id and b.get("key"):
                    self._deleted[b["key"]] = now
            # régi bejegyzések takarítása
            self._deleted = {k: t for k, t in self._deleted.items()
                             if now - t < DELETED_TTL}
            self._placed = [b for b in self._placed if b["id"] != bet_id]
            self._save()

    # ---------- automatikus lezárás (élő-állás elkapás) ----------
    def _settle_pass(self, now):
        """Egy eredmény-lekérő kör. Visszaad egy összegzést a felület felé."""
        summary = {"settled": 0, "live_seen": 0, "needs_manual": 0,
                   "no_key": 0, "pending": 0}
        if not self.cfg.get("results", {}).get("auto_settle", True):
            summary["disabled"] = True
            return summary
        rcfg = self.cfg.get("results", {})
        min_after = rcfg.get("min_minutes_after_start", 90) * 60
        grace = rcfg.get("absent_grace_sec", 180)
        with self._lock:
            pend = [b for b in self._placed if b.get("status") == "pending"]
            summary["pending"] = len(pend)
            summary["no_key"] = sum(1 for b in pend if not b.get("vegas_id"))
            sports = {b["sport_id"] for b in pend
                      if b.get("vegas_id") and b.get("sport_id")}
        # 1) ÉLŐ feed (Altenar): csak a vegas_id-vel rendelkező tételekre, és csak
        #    akkor hasznos, ha az app fut a meccs vége környékén.
        live = {}
        for sid in sports:
            try:
                live[sid] = self.vegas.fetch_live_scores(sid)
            except Exception:
                live[sid] = {}
        with self._lock:
            changed = False
            for b in self._placed:
                if b.get("status") != "pending" or not b.get("vegas_id"):
                    continue
                ev = live.get(b.get("sport_id"), {}).get(b["vegas_id"])
                if ev and ev.get("score"):
                    b["live_score"] = ev["score"]
                    b["live_seen_ts"] = now
                    b["needs_manual"] = False
                    summary["live_seen"] += 1
                    changed = True
                    continue
                st = b.get("start_ts")
                started_long = st is not None and now - st > min_after
                seen = b.get("live_seen_ts")
                if b.get("live_score") and seen and now - seen > grace and started_long:
                    hs, as_ = b["live_score"][0], b["live_score"][1]
                    res = results.grade(b.get("sport_id"), b.get("subkey", ""), hs, as_)
                    b["final_score"] = list(b["live_score"])
                    if res:
                        b["status"] = res
                        b["settled_ts"] = now
                        b["settle_source"] = "auto"
                        b["needs_manual"] = False
                        summary["settled"] += 1
                    else:
                        b["needs_manual"] = True
                        summary["needs_manual"] += 1
                    changed = True
                elif started_long and not b.get("live_score"):
                    # az élő feed nem érte el; a visszamenőleges lépés még próbálkozik
                    pass
            if changed:
                self._save()
        # 2) VISSZAMENŐLEGES (TheSportsDB): minden lejárt nyitott tételre, akkor is,
        #    ha az app nem futott a meccs alatt (a véglegesen lezárt meccs eredménye
        #    így is lekérhető). Ez a fő lezáró mechanizmus a papír-tételekhez.
        if self.retroactive:
            try:
                self._retroactive_settle(now, min_after, summary)
            except Exception as e:
                summary["retro_error"] = str(e)
        # 3) AUTO-VOID: amit pár napon belül sehol nem találtunk meg (pl. obskúrus
        #    alsóbb osztályú meccs, amit az ingyenes eredmény-forrás nem ismer),
        #    automatikusan 'semmis'-re tesszük, hogy SOHA ne maradjon kézi teendő.
        #    A void se nem nyert, se nem vesztett -> nem torzítja a statisztikát.
        rcfg = self.cfg.get("results", {})
        auto_void = rcfg.get("auto_void", True)
        void_after = rcfg.get("auto_void_after_days", 3) * 86400
        with self._lock:
            changed = False
            for b in self._placed:
                if b.get("status") != "pending":
                    continue
                st = b.get("start_ts") or _iso_ts(b.get("start"))
                # ha a meccs még el sem (kvázi) kezdődött rég, ne nyúljunk hozzá
                if st is not None and now - st <= min_after:
                    continue
                # öregség-referencia: a kezdés, vagy ha nincs, a megrakás ideje
                # (így a kezdés nélküli régi tételek is automatikusan rendeződnek)
                ref = st if st is not None else b.get("ts")
                if ref is None:
                    continue
                if auto_void and now - ref > void_after:
                    b["status"] = "void"
                    b["settled_ts"] = now
                    b["settle_source"] = "auto-void"
                    b["needs_manual"] = False
                    summary["voided"] = summary.get("voided", 0) + 1
                    changed = True
                elif not b.get("needs_manual"):
                    # még a türelmi időn belül – várunk, hátha megjelenik az eredmény
                    b["needs_manual"] = True
                    summary["needs_manual"] += 1
                    changed = True
            if changed:
                self._save()
        return summary

    def _retroactive_settle(self, now, min_after, summary):
        """A lejárt, még nyitott tételeket a TheSportsDB végeredményéből zárja le."""
        from .sportsdb import TENNIS_SPORT_ID
        with self._lock:
            # jelölt: a nyitottak ÉS az auto-void tételek (utóbbiak önjavító
            # újraellenőrzése – ha időközben egy forrásban már megvan az eredmény,
            # a void-ot valódi won/lost-ra frissítjük)
            cand = [b for b in self._placed
                    if (b.get("status") == "pending"
                        or (b.get("status") == "void"
                            and b.get("settle_source") == "auto-void"))
                    and b.get("sport_id") in self.sportsdb.RETRO_SPORT_IDS
                    and b.get("event") and (b.get("start_ts") or b.get("start"))
                    and (b.get("start_ts") is None or now - b["start_ts"] > min_after)]
        for b in cand:
            ev = b.get("event", "")
            home, _, away = ev.partition(" - ")
            sid = b.get("sport_id")
            src = "sportsdb"
            try:
                if sid == TENNIS_SPORT_ID:
                    # elsődleges: tennisexplorer (teljes mezőny, Challenger/ITF is),
                    # tartalék: TheSportsDB (csak nagy tornák)
                    tr = self.tennisexplorer.result(home, away, b.get("start"))
                    src = "tennisexplorer"
                    if not tr:
                        tr = self.sportsdb.tennis_result(home, away, b.get("start"))
                        src = "sportsdb"
                    if not tr:
                        continue
                    hs_set, as_set, _hg, _ag = tr
                    res = results.grade_tennis(b.get("subkey", ""), *tr)
                    final = [hs_set, as_set]   # a 'score' teniszben a szett-eredmény
                else:
                    fs = self.sportsdb.final_score(sid, home, away, b.get("start"))
                    if not fs:
                        continue
                    final = [fs[0], fs[1]]
                    res = results.grade(sid, b.get("subkey", ""), fs[0], fs[1])
            except Exception:
                continue
            with self._lock:
                b["final_score"] = final
                if res:
                    b["status"] = res
                    b["settled_ts"] = now
                    b["settle_source"] = src
                    b["needs_manual"] = False
                    summary["settled"] += 1
                elif b.get("status") == "pending":
                    # van eredmény, de nem értékelhető automatikusan -> később void/kézi
                    b["needs_manual"] = True
                self._save()

    def check_results(self):
        """Manuális eredmény-ellenőrzés (gombról). Azonnal lefuttat egy lezáró kört."""
        now = time.time()
        try:
            s = self._settle_pass(now)
        except Exception as e:
            return {"ok": False, "reason": str(e)}
        s["ok"] = True
        s["ts"] = now_iso()
        return s

    # ---------- email-válaszok beolvasása ----------
    def check_inbox(self):
        if not self.cfg.get("inbox", {}).get("enabled", True):
            self.last_inbox_result = {"ok": False, "reason": "inbox kikapcsolva"}
            return self.last_inbox_result
        from .inbox import InboxReader
        reader = InboxReader(self.cfg)
        if not reader.configured():
            self.last_inbox_result = {
                "ok": False,
                "reason": "nincs IMAP jelszó – tedd a Gmail app-jelszót a config.json "
                          "notify.smtp_password mezőjébe"}
            return self.last_inbox_result
        actions = reader.fetch_actions()
        placed = skipped = 0
        for a in actions:
            if a["action"] == "ok":
                if self.place_from_email(a["bet"]):
                    placed += 1
            else:
                skipped += 1
        self.last_inbox_result = {"ok": True, "placed": placed,
                                  "skipped": skipped, "seen": len(actions),
                                  "ts": now_iso()}
        if placed:
            print(f"[inbox] {placed} új megrakott tét beolvasva emailből.")
        return self.last_inbox_result

    # ---------- Telegram gomb-válaszok ----------
    def check_telegram(self):
        """A Telegram ✅/❌ gombnyomások beolvasása (getUpdates) és mentése.

        A 'Megraktam' gomb üzenetébe rejtett (spoiler) tokenből visszafejti a
        fogadást – ugyanúgy, mint az emailes 'inbox'. Így a felhő (vagy a helyi
        app) által küldött tippet telefonról egy koppintással elmentheted; a
        Telegram a gombnyomást ~24 órán át tárolja, így kikapcsolt laptopnál is
        megmarad, amíg az app legközelebb fut."""
        if not self.telegram.configured():
            self.last_tg_result = {"ok": False, "reason": "Telegram nincs beállítva"}
            return self.last_tg_result
        # A háttér long-poll szál olvassa a gombokat azonnal; kézi hívásnál ne
        # pollozzunk párhuzamosan (Telegram getUpdates 409), csak az állapotot adjuk.
        if self._tg_thread_running:
            return self.last_tg_result or {"ok": True, "placed": 0, "skipped": 0,
                                           "ignored": 0, "seen": 0,
                                           "note": "a háttérfigyelő olvassa a gombokat"}
        updates = self.telegram.get_updates(offset=self._tg_offset)
        return self._handle_tg_updates(updates)

    def _telegram_loop(self):
        """Háttérszál: long-poll getUpdates → a gombnyomás AZONNAL feldolgozódik."""
        self._tg_thread_running = True
        try:
            while not self._stop.is_set():
                try:
                    updates = self.telegram.get_updates(offset=self._tg_offset, timeout=25)
                    if updates:
                        self._handle_tg_updates(updates)
                except Exception as e:
                    self.last_tg_result = {"ok": False, "reason": str(e)}
                    self._stop.wait(5)
        finally:
            self._tg_thread_running = False

    def _handle_tg_updates(self, updates):
        placed = skipped = ignored = 0
        owner = str(self.telegram.chat_id)
        changed = False
        for u in updates or []:
            self._tg_offset = u["update_id"] + 1
            changed = True
            cb = u.get("callback_query")
            if not cb:
                # sima szöveges üzenet (pl. /stat parancs)
                m = u.get("message") or {}
                mtext = (m.get("text") or "").strip()
                mchat = str((m.get("chat") or {}).get("id", ""))
                if mtext and (not owner or mchat == owner):
                    self._handle_tg_command(mtext, mchat)
                continue
            # Csak a TULAJDONOS chat_id-jából fogadunk el gombnyomást – más ne
            # tudja a botot használni / a fogadásaidat piszkálni.
            from_chat = str(((cb.get("message") or {}).get("chat") or {}).get("id", ""))
            from_user = str((cb.get("from") or {}).get("id", ""))
            if owner and owner not in (from_chat, from_user):
                ignored += 1
                self.telegram.answer_callback(cb["id"], "Nincs jogosultság.")
                continue
            data = cb.get("data", "")
            msg = cb.get("message") or {}
            text = msg.get("text", "")
            bet = bettoken.decode(text)
            msg_chat = (msg.get("chat") or {}).get("id")
            msg_id = msg.get("message_id")
            # rövid összefoglaló a döntés-nyomhoz
            if bet:
                summ = f"{bet.get('event','')} – {bet.get('tip','')} @ {bet.get('odds','')}"
            else:
                summ = (text.split("\n", 1)[0] if text else "")
            if data == "vbok" and bet:
                if self.place_from_email(bet):
                    placed += 1
                    self.telegram.answer_callback(cb["id"], "Elmentve ✅")
                    self.telegram.edit_text(msg_chat, msg_id, f"✅ <b>Elmentve</b>\n{summ}")
                else:
                    self.telegram.answer_callback(cb["id"], "Már mentve")
                    self.telegram.edit_text(msg_chat, msg_id, f"✅ <b>Már elmentve</b>\n{summ}")
            else:
                skipped += 1
                self.telegram.answer_callback(cb["id"], "Kihagyva")
                self.telegram.edit_text(msg_chat, msg_id, f"❌ <b>Kihagyva</b>\n{summ}")
        if changed:
            self._save()
        self.last_tg_result = {"ok": True, "placed": placed, "skipped": skipped,
                               "ignored": ignored, "seen": len(updates or []),
                               "ts": now_iso()}
        if placed:
            print(f"[telegram] {placed} új megrakott tét beolvasva.")
        return self.last_tg_result

    # ---------- Telegram parancsok ----------
    def _handle_tg_command(self, text, chat_id):
        cmd = text.lower().lstrip("/").split("@")[0].split()[0] if text else ""
        if cmd in ("stat", "stats", "statisztika"):
            self.telegram.send(self._stats_text(), chat_id=chat_id)
        elif cmd in ("start", "help", "sugo", "súgó"):
            self.telegram.send(
                "👋 <b>Value Bet bot</b>\n"
                "Új biztos value tippnél ide küldök értesítést a ✅ Megraktam / "
                "❌ Kihagytam gombokkal.\n\n"
                "Parancs:\n/stat – aktuális statisztika (hozam, találati arány, CLV)",
                chat_id=chat_id)

    def _stats_text(self):
        with self._lock:
            s = self._stats()
            bankroll = self.settings.bankroll
            open_n = sum(1 for b in self._placed if b["status"] == "pending")
        roi = s["roi"]
        sign = "+" if roi >= 0 else ""
        lines = [
            "📊 <b>Value Bet – statisztika</b>",
            f"Hozam (yield): <b>{sign}{roi}%</b>  (egységes tét)",
            f"Találati arány: <b>{s['hit_rate']}%</b>  ({s['won']}/{s['settled']} nyert/lezárt)",
            f"Eredmény (egys. tét): <b>{s['pnl']:,}</b> Ft".replace(",", " "),
            f"Valós P/L (változó tét): <b>{s['real_pnl']:,}</b> Ft".replace(",", " "),
            f"Nyitott fogadás: <b>{open_n}</b>  ({s['open_stake']:,} Ft)".replace(",", " "),
            f"Tőke: <b>{bankroll:,.0f}</b> Ft".replace(",", " "),
        ]
        if s.get("clv_avg") is not None:
            lines.append(f"Átlag CLV: <b>{'+' if s['clv_avg'] >= 0 else ''}{s['clv_avg']}%</b>"
                         f"  ({s['clv_n']} tipp, {s['clv_beat_rate']}% verte a zárót)")
        return "\n".join(lines)

    def _profit(self, b):
        """Valós nyereség/veszteség a TÉNYLEGESEN megrakott téttel (referencia)."""
        if b["status"] == "won":
            return b["stake"] * (b["odds"] - 1)
        if b["status"] == "lost":
            return -b["stake"]
        return 0.0

    @staticmethod
    def _unit_profit(b, unit):
        """Nyereség/veszteség EGYSÉGES (fix) téttel – így a változó tét nem torzít.
        A teljesítményt az méri, hogy minden fogadásra ugyanannyit téve nő-e a balance."""
        if b["status"] == "won":
            return unit * (b["odds"] - 1)
        if b["status"] == "lost":
            return -unit
        return 0.0

    def _stats(self):
        settled = [b for b in self._placed if b["status"] in ("won", "lost")]
        won = sum(1 for b in settled if b["status"] == "won")
        unit = self.settings.unit_stake or self.settings.min_bet or 100
        order = sorted(settled, key=lambda x: x["settled_ts"] or 0)
        # ELSŐDLEGES mérce: egységes tét -> torzítatlan, hogy a picks tényleg nyer-e
        u_pnl = sum(self._unit_profit(b, unit) for b in order)
        u_staked = unit * len(settled)
        # valós (változó tét) – csak referenciaként
        real_pnl = sum(self._profit(b) for b in settled)
        real_staked = sum(b["stake"] for b in settled)
        # HOZAM-görbe (yield): futó profit / futó megtett tét, %-ban, lezárás sorrendjében.
        # Nem a Ft-összeg számít, hanem a stake-független hozam% (ez a valódi edge-mérő).
        curve = [{"i": 0, "yield": 0.0}]
        c_pnl = c_staked = 0.0
        for i, b in enumerate(order, 1):
            c_pnl += self._unit_profit(b, unit)
            c_staked += unit
            curve.append({"i": i, "yield": round(100 * c_pnl / c_staked, 1) if c_staked else 0.0})
        # CLV: a megfogott odds verte-e az éles iroda záró (valós) oddsát.
        clvs = [b["clv_pct"] for b in self._placed if b.get("clv_pct") is not None]
        clv_avg = round(sum(clvs) / len(clvs), 2) if clvs else None
        beat = sum(1 for c in clvs if c > 0)
        return {
            "placed_total": len(self._placed), "settled": len(settled),
            "won": won, "lost": len(settled) - won,
            "hit_rate": round(100 * won / len(settled), 1) if settled else 0,
            "unit_stake": round(unit),
            "staked": round(u_staked), "pnl": round(u_pnl),
            "roi": round(100 * u_pnl / u_staked, 1) if u_staked else 0,
            "real_pnl": round(real_pnl), "real_staked": round(real_staked),
            "real_roi": round(100 * real_pnl / real_staked, 1) if real_staked else 0,
            "open_stake": round(sum(b["stake"] for b in self._placed if b["status"] == "pending")),
            "curve": curve,
            "clv_avg": clv_avg, "clv_n": len(clvs),
            "clv_beat_rate": round(100 * beat / len(clvs), 1) if clvs else None,
        }

    # ---------- felület felé ----------
    def snapshot(self):
        with self._lock:
            s = self.settings
            sports, markets = set(s.sports), set(s.markets)
            bets = []
            for rec in self._bets.values():
                if rec["sport_id"] not in sports or rec["market"] not in markets:
                    continue
                if rec["odds"] < s.min_odds or rec["odds"] > s.max_odds:
                    continue
                if rec["value_pct"] < s.min_value_pct:
                    continue
                now_t = time.time()
                stable_sec = now_t - rec.get("stable_since", rec["last_seen"])
                hours_to_start = ((rec["start_ts"] - now_t) / 3600.0
                                  if rec.get("start_ts") else None)
                solid = self._is_solid(rec, now_t)
                if s.only_solid and not solid:
                    continue
                out = {k: v for k, v in rec.items() if k != "fair_p"}
                out["age_sec"] = round(now_t - rec["last_seen"], 1)
                out["stable_sec"] = round(stable_sec)
                out["hours_to_start"] = round(hours_to_start, 1) if hours_to_start is not None else None
                out["status"] = "ÉLŐ" if rec["valid"] else "LEJÁRT"
                out["stake"] = self._stake(rec["fair_p"], rec["odds"])
                out["solid"] = solid
                bets.append(out)
            bets.sort(key=lambda r: (not r["valid"], -r["value_pct"]))
            meta = dict(self.meta)
            meta["now"] = time.time()
            meta["inbox"] = self.last_inbox_result
            meta["telegram"] = self.last_tg_result
            meta["telegram_configured"] = self.telegram.configured()
            now_t = time.time()
            placed_out = []
            for b in reversed(self._placed):
                o = dict(b)
                if o.get("status") == "pending":
                    st = o.get("start_ts") or _iso_ts(o.get("start"))
                    if o.get("needs_manual") or (st and now_t - st > 2 * 3600):
                        o["needs_manual"] = True
                placed_out.append(o)
            return {"bets": bets, "settings": s.to_dict(), "meta": meta,
                    "placed": placed_out, "stats": self._stats()}
