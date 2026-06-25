"""Felhős PAPÍR-FOGADÁS – ütemezetten fut (GitHub Actions), laptop nélkül is.

Egy futás:
  1) keres biztos value tippeket (vegas.hu + Pinnacle, a notify_cron.scan logikája),
  2) a ≤ paper.max_odds (alap 2.0) tippeket 'megrakja' papíron (tét-sapkával),
     tartalom-alapú deduppal (ugyanaz a meccs+tipp ne kerüljön be kétszer),
  3) lezárja a lejárt nyitottakat (TheSportsDB foci/kosár/hoki + tennisexplorer
     tenisz; amit pár nap után sehol nem talál -> auto-void),
  4) naponta EGYSZER (report.hour után) Telegram-jelentést küld a statisztikáról.

A 'főkönyv' a `paper_data.json` (a workflow commitolja vissza a PRIVÁT repóba),
így a statisztika a futások közt megmarad. A Telegram token/chat a TELEGRAM_TOKEN/
TELEGRAM_CHAT_ID környezeti változókból (GitHub secrets) jön.
"""
import json
import os
import time
from datetime import datetime, timezone, date

from valuebet.http import Http
from valuebet.sportsdb import SportsDBClient, TENNIS_SPORT_ID
from valuebet.tennisexplorer import TennisExplorerClient
from valuebet.telegram import TelegramNotifier
from valuebet import results, value as V
import notify_cron

LEDGER = os.environ.get("PAPER_LEDGER", "paper_data.json")


# ---------- config / ledger ----------
def load_cfg():
    path = "config.json" if os.path.exists("config.json") else "config.example.json"
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    return notify_cron.inject_env(cfg)


def load_ledger():
    if os.path.exists(LEDGER):
        try:
            with open(LEDGER, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
    else:
        d = {}
    d.setdefault("placed", [])
    d.setdefault("papered", {})
    d.setdefault("last_report_date", None)
    d.setdefault("next_id", max([b.get("id", 0) for b in d["placed"]], default=0) + 1)
    return d


def save_ledger(d):
    with open(LEDGER, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def _iso_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# ---------- tét (Kelly + sapka) ----------
def stake_capped(cfg, fair_p, odds):
    live = cfg.get("live", {})
    bankroll = float(live.get("bankroll", 0))
    if bankroll <= 0:
        return 0
    full = V.kelly_fraction(fair_p, odds) * float(cfg.get("value", {}).get("kelly_fraction", 0.25))
    if full <= 0:
        return 0
    raw = bankroll * full
    cap_pct = float(live.get("max_stake_pct", 5.0))
    if cap_pct > 0:
        raw = min(raw, bankroll * cap_pct / 100.0)
    st = int(round(raw / 100.0) * 100)
    min_bet = float(live.get("min_bet", 100))
    if st < min_bet:
        st = int(min_bet)
    return st


# ---------- megrakás ----------
def place_new(cfg, ledger):
    found, now = notify_cron.scan(cfg)
    max_odds = float(cfg.get("paper", {}).get("max_odds", 2.0))
    placed = 0
    for ck, v, b in found:
        if b["odds"] > max_odds:
            continue
        if ck in ledger["papered"]:
            continue
        ledger["papered"][ck] = now
        d = notify_cron.bet_dict(cfg, v, b)
        start = d["start"]
        rec = {
            "id": ledger["next_id"], "ts": now, "key": d["key"],
            "sport": d["sport"], "sport_id": v.sport_id, "subkey": b["subkey"],
            "event": d["event"], "market": b.get("market", ""),
            "market_name": b["market_name"], "tip": b["tip"],
            "odds": float(b["odds"]), "stake": int(stake_capped(cfg, b["fair_p"], b["odds"])),
            "value_pct": b["value_pct"], "fair_pct": d["fair_pct"],
            "start": start, "start_ts": _iso_ts(start),
            "status": "pending", "settled_ts": None, "source": "auto",
            "final_score": None, "settle_source": None, "needs_manual": False,
        }
        ledger["next_id"] += 1
        ledger["placed"].append(rec)
        placed += 1
    # régi tartalom-kulcsok takarítása
    cutoff = now - 45 * 86400
    ledger["papered"] = {k: t for k, t in ledger["papered"].items() if t >= cutoff}
    print(f"[paper] {placed} uj tipp megrakva (<={max_odds} odds), osszes talalat: {len(found)}.")
    return placed


# ---------- lezárás (visszamenőleges + auto-void) ----------
def settle(cfg, ledger, sportsdb, te):
    rcfg = cfg.get("results", {})
    min_after = rcfg.get("min_minutes_after_start", 90) * 60
    auto_void = rcfg.get("auto_void", True)
    void_after = rcfg.get("auto_void_after_days", 3) * 86400
    now = time.time()
    settled = voided = 0

    for b in ledger["placed"]:
        status = b.get("status")
        if not (status == "pending" or (status == "void" and b.get("settle_source") == "auto-void")):
            continue
        if b.get("sport_id") not in sportsdb.RETRO_SPORT_IDS:
            continue
        st = b.get("start_ts") or _iso_ts(b.get("start"))
        if st is not None and now - st <= min_after:
            continue
        ev = b.get("event", "")
        home, _, away = ev.partition(" - ")
        sid = b.get("sport_id")
        src = "sportsdb"
        try:
            if sid == TENNIS_SPORT_ID:
                tr = te.result(home, away, b.get("start"))
                src = "tennisexplorer"
                if not tr:
                    tr = sportsdb.tennis_result(home, away, b.get("start"))
                    src = "sportsdb"
                if not tr:
                    continue
                res = results.grade_tennis(b.get("subkey", ""), *tr)
                final = [tr[0], tr[1]]
            else:
                fs = sportsdb.final_score(sid, home, away, b.get("start"))
                if not fs:
                    continue
                res = results.grade(sid, b.get("subkey", ""), fs[0], fs[1])
                final = [fs[0], fs[1]]
        except Exception:
            continue
        b["final_score"] = final
        if res:
            b["status"] = res
            b["settled_ts"] = now
            b["settle_source"] = src
            b["needs_manual"] = False
            settled += 1

    # auto-void: amit pár nap után sehol nem találtunk
    if auto_void:
        for b in ledger["placed"]:
            if b.get("status") != "pending":
                continue
            st = b.get("start_ts") or _iso_ts(b.get("start"))
            if st is not None and now - st <= min_after:
                continue
            ref = st if st is not None else b.get("ts")
            if ref is None:
                continue
            if now - ref > void_after:
                b["status"] = "void"
                b["settled_ts"] = now
                b["settle_source"] = "auto-void"
                b["needs_manual"] = False
                voided += 1
    print(f"[settle] {settled} lezárva, {voided} auto-void.")
    return settled, voided


# ---------- statisztika + napi jelentés ----------
def compute_stats(ledger):
    placed = ledger["placed"]
    settled = [b for b in placed if b["status"] in ("won", "lost")]
    won = sum(1 for b in settled if b["status"] == "won")
    def profit(b):
        if b["status"] == "won":
            return b["stake"] * (b["odds"] - 1)
        if b["status"] == "lost":
            return -b["stake"]
        return 0.0
    unit = 100
    def uprofit(b):
        if b["status"] == "won":
            return unit * (b["odds"] - 1)
        if b["status"] == "lost":
            return -unit
        return 0.0
    real_pnl = sum(profit(b) for b in settled)
    u_pnl = sum(uprofit(b) for b in settled)
    u_staked = unit * len(settled)
    return {
        "settled": len(settled), "won": won, "lost": len(settled) - won,
        "hit_rate": round(100 * won / len(settled), 1) if settled else 0,
        "real_pnl": round(real_pnl),
        "roi": round(100 * u_pnl / u_staked, 1) if u_staked else 0,
        "open": sum(1 for b in placed if b["status"] == "pending"),
        "void": sum(1 for b in placed if b["status"] == "void"),
    }


def report_text(cfg, ledger):
    s = compute_stats(ledger)
    bankroll = float(cfg.get("live", {}).get("bankroll", 0)) or 0
    growth = (100 * s["real_pnl"] / bankroll) if bankroll else 0
    gsign = "+" if growth >= 0 else ""
    ysign = "+" if s["roi"] >= 0 else ""
    lines = [
        "📊 <b>Napi value-bet jelentés</b> (felhő)",
        "",
        f"💼 Portfólió: <b>{gsign}{growth:.2f}%</b>  "
        f"({s['real_pnl']:+,} Ft / {bankroll:,.0f} Ft tőke)".replace(",", " "),
        f"📈 Hozam (yield, egys. tét): <b>{ysign}{s['roi']}%</b>",
        f"🎯 Találati arány: <b>{s['hit_rate']}%</b>  ({s['won']}/{s['settled']} nyert/lezárt)",
        f"🟢 Nyitott (papír) tétel: <b>{s['open']}</b>",
        "",
        "<i>A szoftver automatikusan 'megrakja' a ≤2.00 oddsú biztos value "
        "tippeket; valódi fogadás nem történik.</i>",
    ]
    return "\n".join(lines)


def maybe_report(cfg, ledger, tg):
    if not cfg.get("report", {}).get("enabled", True) or not tg.configured():
        return False
    hour = int(cfg.get("report", {}).get("hour", 9))
    if datetime.now().hour < hour:
        return False
    today = date.today().isoformat()
    if ledger.get("last_report_date") == today:
        return False
    try:
        tg.send(report_text(cfg, ledger))
    except Exception as e:
        print(f"[report] HIBA: {e}")
        return False
    ledger["last_report_date"] = today
    print(f"[report] napi jelentés elküldve ({today}).")
    return True


# ---------- belépés ----------
def main():
    cfg = load_cfg()
    http = Http(verify_ssl=cfg.get("http", {}).get("verify_ssl", True), delay_sec=0)
    sportsdb = SportsDBClient(http, cfg)
    te = TennisExplorerClient(http, cfg)
    tg = TelegramNotifier(cfg)

    ledger = load_ledger()
    try:
        place_new(cfg, ledger)
    except Exception as e:
        print(f"[paper] scan/megrakás hiba: {e}")
    settle(cfg, ledger, sportsdb, te)
    maybe_report(cfg, ledger, tg)
    save_ledger(ledger)
    s = compute_stats(ledger)
    print(f"[kész] lezárt={s['settled']} nyert={s['won']} void={s['void']} "
          f"nyitott={s['open']} yield={s['roi']}%")


if __name__ == "__main__":
    main()
