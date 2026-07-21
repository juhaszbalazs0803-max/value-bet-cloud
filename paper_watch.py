"""Folyamatos (közel valós idejű) felhős PAPÍR-FOGADÁS.

A `paper_cron` egyszer-fut logikáját ismétli egy loopban: ~90 mp-enként keres és
AZONNAL megrakja az új, kritériumnak megfelelő (solid value + odds <= paper.max_odds)
tippeket – így a rövid életű, két scan közt eltűnő tippeket is elkapja (nem csak
óránként néz rá). A loop ~53 percig fut, közben időnként commitolja a ledgert; a
GitHub Actions óránként újraindítja, így a lefedettség közel folyamatos.

Ehhez a repónak PUBLIKUSnak kell lennie (korlátlan Actions-perc); privát repón a
2000 perc/hó keretet kimerítené.

Env:
  POLL_SEC          – scan-gyakoriság (alap 90)
  MAX_RUNTIME_SEC   – meddig fusson, majd tiszta kilépés (alap 3180 = 53 perc)
  COMMIT_EVERY_SEC  – milyen sűrűn mentse/pusholja a ledgert (alap 600 = 10 perc)
  SETTLE_EVERY_SEC  – milyen sűrűn próbáljon eredményt lezárni (alap 600)
"""
import html
import os
import subprocess
import time
from datetime import datetime

import notify_cron
import paper_cron as P
from valuebet.http import Http
from valuebet.sportsdb import SportsDBClient
from valuebet.espn import ESPNClient
from valuebet.tennisexplorer import TennisExplorerClient
from valuebet.telegram import TelegramNotifier

POLL = int(os.environ.get("POLL_SEC", "90"))
MAX_RUNTIME = int(os.environ.get("MAX_RUNTIME_SEC", "3180"))
COMMIT_EVERY = int(os.environ.get("COMMIT_EVERY_SEC", "600"))
SETTLE_EVERY = int(os.environ.get("SETTLE_EVERY_SEC", "600"))
# riasztás: ennyi EGYMÁST KÖVETŐ rossz scan-kör után szól Telegramon
ALERT_AFTER_BAD = int(os.environ.get("ALERT_AFTER_BAD", "3"))
# két riasztás közt legalább ennyi idő (a cooldown a ledgerben perzisztál,
# így az óránkénti workflow-újraindulás nem nullázza)
ALERT_COOLDOWN = int(os.environ.get("ALERT_COOLDOWN_SEC", str(6 * 3600)))


# ---------- rendszer-egészség + Telegram hibariasztás ----------
def scan_problems(scan_exc):
    """Az utolsó scan-kör problémái (lista, üres = egészséges).

    Csak RENDSZERSZINTŰ bajra riasztunk (minden forrás elhasal / 0 esemény);
    az egy-egy sportot érintő átmeneti hiba nem riasztás, azt a napi jelentés
    egészség-sora mutatja."""
    if scan_exc is not None:
        return [f"scan kivétel: {scan_exc}"]
    h = notify_cron.LAST_SCAN_HEALTH
    if not h:
        return []
    probs = []
    errs = h.get("sports_err") or {}
    if not h.get("sports_ok"):
        first = "; ".join(f"[{k}] {v}" for k, v in list(errs.items())[:2]) or "?"
        probs.append(f"MINDEN sport lekérése elhasalt ({first})")
    else:
        if not h.get("vegas_events"):
            probs.append("a vegas.hu (Altenar) 0 eseményt adott — API-változás?")
        if not h.get("pinnacle_events"):
            probs.append("a Pinnacle 0 eseményt adott — API/kulcs-változás?")
    return probs


def update_health(ledger, tg, problems, consec_bad, settle_err=None):
    """Egészség-állapot a ledgerbe (a napi jelentés innen olvassa) + riasztás.

    Riasztás: ALERT_AFTER_BAD egymást követő rossz kör után, ALERT_COOLDOWN
    ránctartással; helyreálláskor egyszeri ✅ üzenet."""
    hstate = ledger.setdefault("health", {})
    h = notify_cron.LAST_SCAN_HEALTH or {}
    hstate["last_scan"] = {
        "ts": h.get("ts") or time.time(), "ok": not problems,
        "vegas_events": h.get("vegas_events", 0),
        "pinnacle_events": h.get("pinnacle_events", 0),
        "sports_err": h.get("sports_err") or {},
        "problems": problems, "settle_err": settle_err,
    }
    if not tg.configured():
        return
    now = time.time()
    try:
        if problems and consec_bad >= ALERT_AFTER_BAD:
            if now - float(hstate.get("last_alert_ts") or 0) >= ALERT_COOLDOWN:
                body = "\n".join("• " + html.escape(p) for p in problems)
                if settle_err:
                    body += "\n• " + html.escape(f"eredmény-lezárás hibázik: {settle_err}")
                tg.send("🔴 <b>Value-bet rendszerhiba</b>\n" + body +
                        f"\n\n<i>{consec_bad} egymást követő körben. Legfeljebb "
                        f"{ALERT_COOLDOWN // 3600} óránként egy riasztás; "
                        "helyreálláskor külön üzenet jön.</i>")
                hstate["last_alert_ts"] = now
                hstate["alert_active"] = True
                print(f"[health] RIASZTÁS kiküldve: {problems}")
        elif not problems and hstate.get("alert_active"):
            tg.send("✅ <b>Value-bet:</b> az adatgyűjtés helyreállt, a rendszer megy tovább.")
            hstate["alert_active"] = False
            print("[health] helyreállás-üzenet kiküldve")
    except Exception as e:
        print(f"[health] riasztás-küldés hiba: {e}")


def git_push():
    """A ledger commitolása + pushja (ha változott). Az Actions checkout által
    beállított hitelesítést használja; hiba esetén csak logol, nem áll meg."""
    try:
        subprocess.run(["git", "add", "paper_data.json"], check=False)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
            return  # nincs ledger-változás
        subprocess.run(["git", "commit", "-m", "paper: ledger frissites [skip ci]"],
                       check=False)
        # A távoli main időközben előrébb léphetett (keepalive heartbeat, sorba
        # állított futás pushja) -> ELŐBB rebase-eljük rá a ledger-commitot, csak
        # utána pusholunk. Enélkül a push non-fast-forward miatt elutasul és a
        # ledger-frissítés (akár a frissen beállított last_report_date) elveszne,
        # ami a napi jelentés DUPLÁZÓDÁSÁHOZ vezet a következő futásban.
        for attempt in range(3):
            subprocess.run(["git", "pull", "--rebase", "--autostash"],
                           capture_output=True, text=True)
            r = subprocess.run(["git", "push"], capture_output=True, text=True)
            if r.returncode == 0:
                print("[git] ledger pusholva")
                return
            tail = (r.stderr.strip().splitlines() or [""])[-1]
            print(f"[git] push hiba (próba {attempt + 1}/3): {tail}")
            time.sleep(2)
    except Exception as e:
        print(f"[git] hiba: {e}")


def handle_commands(cfg, ledger, tg):
    """Telegram parancsok kiszolgálása a FELHŐBŐL (laptop nélkül is).
    /start, /stat, /stats -> aktuális statisztika. Csak a tulajdonos chatnek.
    Ha a lokális app is pollozza a getUpdates-et, az 409-et ad -> csendben kihagyjuk."""
    if not tg.configured():
        return
    owner = str(cfg.get("telegram", {}).get("chat_id", "")).strip()
    try:
        updates = tg.get_updates(offset=ledger.get("tg_offset"), timeout=0) or []
    except Exception:
        return  # 409 (a lokális app pollozik) vagy hálózati hiba -> kihagyjuk
    for u in updates:
        ledger["tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or u.get("edited_message")
        if not msg:
            continue
        chat = str(msg.get("chat", {}).get("id", ""))
        if owner and chat != owner:
            continue  # csak a tulajdonosnak válaszol
        cmd = (msg.get("text") or "").strip().lower().split("@")[0]
        try:
            if cmd in ("/start", "/stat", "/stats", "/statisztika"):
                tg.send(P.report_text(cfg, ledger))
            elif cmd == "/help":
                tg.send("📊 Parancsok:\n/start vagy /stat – aktuális statisztika\n"
                        "/help – ez a súgó")
        except Exception as e:
            print(f"[cmd] válasz hiba: {e}")


def main():
    cfg = P.load_cfg()
    http = Http(verify_ssl=cfg.get("http", {}).get("verify_ssl", True), delay_sec=0)
    sportsdb = SportsDBClient(http, cfg)
    espn = ESPNClient(http, cfg)
    te = TennisExplorerClient(http, cfg)
    tg = TelegramNotifier(cfg)

    ledger = P.load_ledger()
    start = time.time()
    last_commit = last_settle = 0.0
    cycles = total_placed = 0
    consec_bad = settle_bad = 0
    settle_err = None
    print(f"Folyamatos paper-figyelo indul: poll={POLL}s, futasido<={MAX_RUNTIME}s, "
          f"ledger tetelek={len(ledger['placed'])}")

    while time.time() - start < MAX_RUNTIME:
        cycle_t = time.time()
        cycles += 1
        handle_commands(cfg, ledger, tg)   # /start, /stat kiszolgálása (felhőből)
        scan_exc = None
        try:
            total_placed += P.place_new(cfg, ledger, tg)
        except Exception as e:
            scan_exc = e
            print(f"[scan] hiba: {e}")
        now = time.time()
        if now - last_settle >= SETTLE_EVERY:
            last_settle = now
            # friss eredmenyekhez a napi cache-t uritjuk
            sportsdb._cache.clear(); espn._cache.clear(); te._cache.clear()
            try:
                P.settle(cfg, ledger, sportsdb, te, espn)
                settle_bad = 0
                settle_err = None
            except Exception as e:
                settle_bad += 1
                if settle_bad >= 2:      # egyszeri botlásra nem riasztunk
                    settle_err = str(e)[:200]
                print(f"[settle] hiba: {e}")
            P.maybe_report(cfg, ledger, tg)
        problems = scan_problems(scan_exc)
        consec_bad = consec_bad + 1 if problems else 0
        update_health(ledger, tg, problems, consec_bad, settle_err)
        if now - last_commit >= COMMIT_EVERY:
            last_commit = now
            P.save_ledger(ledger)
            git_push()
        dt = time.time() - cycle_t
        time.sleep(max(5, POLL - dt))

    # zaras: utolso lezaras + jelentes + mentes
    sportsdb._cache.clear(); espn._cache.clear(); te._cache.clear()
    try:
        P.settle(cfg, ledger, sportsdb, te, espn)
    except Exception as e:
        print(f"[settle] hiba: {e}")
    P.maybe_report(cfg, ledger, tg)
    P.save_ledger(ledger)
    git_push()
    s = P.compute_stats(ledger)
    print(f"[{datetime.now():%H:%M:%S}] vege: {cycles} kor, {total_placed} uj megrakva, "
          f"lezart={s['settled']} nyert={s['won']} void={s['void']} nyitott={s['open']} "
          f"yield={s['roi']}%")


if __name__ == "__main__":
    main()
