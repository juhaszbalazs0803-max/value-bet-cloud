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
import os
import subprocess
import time
from datetime import datetime

import paper_cron as P
from valuebet.http import Http
from valuebet.sportsdb import SportsDBClient
from valuebet.tennisexplorer import TennisExplorerClient
from valuebet.telegram import TelegramNotifier

POLL = int(os.environ.get("POLL_SEC", "90"))
MAX_RUNTIME = int(os.environ.get("MAX_RUNTIME_SEC", "3180"))
COMMIT_EVERY = int(os.environ.get("COMMIT_EVERY_SEC", "600"))
SETTLE_EVERY = int(os.environ.get("SETTLE_EVERY_SEC", "600"))


def git_push():
    """A ledger commitolása + pushja (ha változott). Az Actions checkout által
    beállított hitelesítést használja; hiba esetén csak logol, nem áll meg."""
    try:
        subprocess.run(["git", "add", "paper_data.json"], check=False)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
            subprocess.run(["git", "commit", "-m", "paper: ledger frissites [skip ci]"],
                           check=False)
            r = subprocess.run(["git", "push"], capture_output=True, text=True)
            print("[git] ledger pusholva" if r.returncode == 0 else f"[git] push hiba: {r.stderr.strip()}")
    except Exception as e:
        print(f"[git] hiba: {e}")


def main():
    cfg = P.load_cfg()
    http = Http(verify_ssl=cfg.get("http", {}).get("verify_ssl", True), delay_sec=0)
    sportsdb = SportsDBClient(http, cfg)
    te = TennisExplorerClient(http, cfg)
    tg = TelegramNotifier(cfg)

    ledger = P.load_ledger()
    start = time.time()
    last_commit = last_settle = 0.0
    cycles = total_placed = 0
    print(f"Folyamatos paper-figyelo indul: poll={POLL}s, futasido<={MAX_RUNTIME}s, "
          f"ledger tetelek={len(ledger['placed'])}")

    while time.time() - start < MAX_RUNTIME:
        cycle_t = time.time()
        cycles += 1
        try:
            total_placed += P.place_new(cfg, ledger)
        except Exception as e:
            print(f"[scan] hiba: {e}")
        now = time.time()
        if now - last_settle >= SETTLE_EVERY:
            last_settle = now
            # friss eredmenyekhez a napi cache-t uritjuk
            sportsdb._cache.clear(); te._cache.clear()
            try:
                P.settle(cfg, ledger, sportsdb, te)
            except Exception as e:
                print(f"[settle] hiba: {e}")
            P.maybe_report(cfg, ledger, tg)
        if now - last_commit >= COMMIT_EVERY:
            last_commit = now
            P.save_ledger(ledger)
            git_push()
        dt = time.time() - cycle_t
        time.sleep(max(5, POLL - dt))

    # zaras: utolso lezaras + jelentes + mentes
    sportsdb._cache.clear(); te._cache.clear()
    try:
        P.settle(cfg, ledger, sportsdb, te)
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
