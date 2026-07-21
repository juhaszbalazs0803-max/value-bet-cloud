"""Felhős VALÓDI automata fogadás-megrakó a vegas.hu-ra (GitHub Actions).

FONTOS – ELŐSZÖR PRÓBA (DRY-RUN):
A vegas.hu magyar, engedélyköteles iroda; a GitHub Actions AMERIKAI IP-ről fut,
ezért a belépés captcha/geo-blokkba ütközhet. Ezért az `autobet.dry_run` alapból
TRUE: a bot belép, megkeresi a tippet, beírja a tétet, de NEM erősíti meg, és
minden lépésről képernyőképet ment (a workflow artifactként feltölti). Csak ha a
képernyőképeken látjuk, hogy az amerikai IP-ről is sikerül a belépés + eljut a
szelvényig, akkor váltunk éles rakásra (dry_run=false + AUTOBET_LIVE=1 secret).

A megrakó-logika a `notify_cron.scan()` biztos tippjeire épül (ugyanaz, amit a
papír-bot használ), a tényleges kattintást/tétbeírást az `AutoBetter` (Playwright)
végzi. Külön dedup-főkönyv (`autobet_data.json`) — egy tippet csak EGYSZER rak
meg, hiba esetén sincs újrapróba (a dupla tét rosszabb, mint egy kihagyás).

Belépési adatok CSAK környezeti változóból (GitHub secrets), SOHA a publikus
config.json-ból:  VEGAS_USER, VEGAS_PASS.
Éles rakás kapcsoló:  AUTOBET_LIVE=1  (enélkül dry_run marad, akkor is, ha a
config dry_run=false — dupla biztosíték a véletlen éles rakás ellen).

Env:
  POLL_SEC          – scan-gyakoriság (alap 120)
  MAX_RUNTIME_SEC   – meddig fusson, majd tiszta kilépés (alap 3180 = 53 perc)
  COMMIT_EVERY_SEC  – milyen sűrűn mentse/pusholja a dedup-ledgert (alap 600)
  VEGAS_USER, VEGAS_PASS – vegas.hu belépés (secret)
  AUTOBET_LIVE      – '1' => valódi rakás; egyébként dry-run
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID – értesítés (secret)
"""
import json
import os
import subprocess
import time
from datetime import datetime

import notify_cron
import paper_cron as P
from valuebet.autobet import AutoBetter
from valuebet.telegram import TelegramNotifier

LEDGER = os.environ.get("AUTOBET_LEDGER", "autobet_data.json")
POLL = int(os.environ.get("POLL_SEC", "120"))
MAX_RUNTIME = int(os.environ.get("MAX_RUNTIME_SEC", "3180"))
COMMIT_EVERY = int(os.environ.get("COMMIT_EVERY_SEC", "600"))


def load_ledger():
    if os.path.exists(LEDGER):
        try:
            with open(LEDGER, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
    else:
        d = {}
    d.setdefault("realbetted", {})   # tartalom-kulcs -> első megrakás ideje
    d.setdefault("log", [])          # rövid megrakás-napló (dátum, esemény, tét, státusz)
    return d


def save_ledger(d):
    with open(LEDGER, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def git_push():
    try:
        subprocess.run(["git", "add", LEDGER], check=False)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
            return
        subprocess.run(["git", "commit", "-m", "autobet: ledger frissites [skip ci]"],
                       check=False)
        for _ in range(3):
            subprocess.run(["git", "pull", "--rebase", "--autostash"],
                           capture_output=True, text=True)
            r = subprocess.run(["git", "push"], capture_output=True, text=True)
            if r.returncode == 0:
                print("[git] ledger pusholva")
                return
            time.sleep(2)
    except Exception as e:
        print(f"[git] hiba: {e}")


def inject_login(cfg):
    """A vegas.hu belépési adatokat KIZÁRÓLAG env-ből tölti be az autobet
    config-blokkba (a publikus config.json SOHA nem tartalmazza)."""
    ab = cfg.setdefault("autobet", {})
    ab["username"] = os.environ.get("VEGAS_USER", "").strip()
    ab["password"] = os.environ.get("VEGAS_PASS", "")
    ab["headless"] = True   # CI-ben nincs képernyő
    # dupla biztosíték: éles rakás CSAK ha AUTOBET_LIVE=1
    live = os.environ.get("AUTOBET_LIVE", "").strip().lower() in ("1", "true", "yes", "on")
    ab["dry_run"] = not live
    return cfg


def _event_name(d):
    return d.get("event", "")


def place_new(cfg, ledger, ab, tg):
    """A friss scan biztos tippjei közül a ≤max_odds ÉS ≥min_value tippeket
    megrakja (dry-run vagy éles), tartalom-kulcs deduppal. Szinkron feldolgozás
    (a felhőben nincs értelme külön szálnak – egyszerre egy böngésző fut)."""
    found, now = notify_cron.scan(cfg)
    min_value = float(cfg.get("notify", {}).get("min_value_pct", 3.0))
    placed = 0
    for ck, v, b in found:
        if b["odds"] > ab.max_odds:
            continue
        if b.get("value_pct", 0) < min_value:
            continue
        if ck in ledger["realbetted"]:
            continue
        d = notify_cron.bet_dict(cfg, v, b)
        stake = ab.stake(b["fair_p"], b["odds"])
        if stake <= 0:
            continue
        # a tippet MOST jelöljük megrakottnak (hiba esetén sincs újrapróba)
        ledger["realbetted"][ck] = now
        job = {"event": _event_name(d), "market": b.get("market", "ml"),
               "tip": b.get("tip", ""), "odds": float(b["odds"]), "stake": int(stake)}
        try:
            status = ab._process(job)   # 'dry' | 'placed' | 'unverified'
        except Exception as e:
            status = f"HIBA: {e}"
        entry = {"ts": datetime.now().isoformat(timespec="seconds"),
                 "event": job["event"], "tip": job["tip"], "odds": job["odds"],
                 "stake": job["stake"], "status": status}
        ledger["log"] = (ledger.get("log", []) + [entry])[-200:]
        placed += 1
        _notify(tg, ab.dry_run, entry)
        print(f"[autobet] {entry}")
    # régi kulcsok takarítása
    cutoff = now - 45 * 86400
    ledger["realbetted"] = {k: t for k, t in ledger["realbetted"].items() if t >= cutoff}
    return placed


def _notify(tg, dry, entry):
    if tg is None or not tg.configured():
        return
    st = entry["status"]
    if dry:
        head = "🧪 <b>Autobet PRÓBA</b> (dry-run, nem valódi)"
    elif st == "placed":
        head = "💰 <b>Valódi tét megrakva</b>"
    elif st == "unverified":
        head = "💰 <b>Tét megrakva</b> (NEM igazolt – nézd meg a fiókban!)"
    else:
        head = "⚠️ <b>Autobet megrakás sikertelen</b> – rakd meg kézzel, ha még value"
    try:
        tg.send(f"{head}\n{entry['event']}\n{entry['tip']} @ {entry['odds']} "
                f"| tét {entry['stake']} Ft\nstátusz: {st}")
    except Exception as e:
        print(f"[autobet] értesítés hiba: {e}")


def main():
    cfg = P.load_cfg()
    cfg = inject_login(cfg)
    ab = AutoBetter(cfg)
    tg = TelegramNotifier(cfg)
    ledger = load_ledger()

    if not cfg["autobet"].get("username") or not cfg["autobet"].get("password"):
        print("[autobet] NINCS VEGAS_USER/VEGAS_PASS secret – kilépés.")
        return
    mode = "ÉLES (valódi pénz!)" if not ab.dry_run else "PRÓBA (dry-run)"
    print(f"Felhős autobet indul: mód={mode}, max_odds={ab.max_odds}, "
          f"poll={POLL}s, futasido<={MAX_RUNTIME}s")

    start = time.time()
    last_commit = 0.0
    total = 0
    while time.time() - start < MAX_RUNTIME:
        t0 = time.time()
        try:
            total += place_new(cfg, ledger, ab, tg)
        except Exception as e:
            print(f"[autobet] scan/megrakás hiba: {e}")
        now = time.time()
        if now - last_commit >= COMMIT_EVERY:
            last_commit = now
            save_ledger(ledger)
            git_push()
        time.sleep(max(10, POLL - (time.time() - t0)))

    save_ledger(ledger)
    git_push()
    print(f"[kész] {total} tipp feldolgozva ({mode}).")


if __name__ == "__main__":
    main()
