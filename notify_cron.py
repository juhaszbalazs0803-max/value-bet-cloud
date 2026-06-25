"""Felhőben (pl. GitHub Actions) futtatható egyszeri kereső + email-küldő.

Lefuttat EGY keresést (vegas.hu + Pinnacle), kiszűri a biztos value beteket,
és emailt küld az ÚJAKRÓL (amikről még nem szólt). A már elküldötteket a
`notified.json` tárolja, hogy ne spammeljen.

Az SMTP belépési adatok KÖRNYEZETI VÁLTOZÓKBÓL jönnek (GitHub secrets), így
nem kerülnek a kódba:  SMTP_USER, SMTP_PASSWORD, TO_EMAIL
"""
import json
import os
import re
from datetime import datetime, timezone

from valuebet.http import Http
from valuebet.vegas import VegasClient, SPORT_NAMES
from valuebet.pinnacle import PinnacleClient, SPORT_MAP
from valuebet.notify import EmailNotifier
from valuebet.telegram import TelegramNotifier, format_tip, BUTTONS
from valuebet import matching, compute, bettoken
from valuebet import value as V

STATE_FILE = "notified.json"
KEEP_SEC = 2 * 86400  # 2 napnál régebbi értesítéseket elfelejtünk


def tip_push_enabled():
    """A felhős PER-TIPP értesítés alapból KI van kapcsolva.

    A koncepció megváltozott: a lokális app automatikusan 'megrakja' (papíron) a
    biztos value tippeket és napi EGY jelentést küld a statisztikáról – nem kell
    per-tipp spam. A felhős azonnali küldést a TIP_PUSH=1 környezeti változóval
    lehet visszakapcsolni (GitHub Actions env / workflow)."""
    return os.environ.get("TIP_PUSH", "0").strip().lower() in ("1", "true", "yes", "on")


def _round_stake(x, step=100):
    return int(round(x / step) * step)


def _norm(s):
    """Név-normalizálás a deduphoz: csak betűk/számok, kisbetűsen."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def dedup_key(v, b):
    """TARTALOM-alapú 'már elküldtem?' kulcs – NEM a változó vegas event-id.

    A csapatnevekből + piacból/tippből (subkey) + a kezdés NAPJÁBÓL áll, így ha
    a meccs eltűnik a feedből majd visszajön (akár ÚJ event-id-vel), ugyanaz a
    kulcs jön ki → nem küldjük ki még egyszer ugyanazt a tippet."""
    day = ""
    if getattr(v, "start", None):
        try:
            day = v.start.strftime("%Y%m%d")
        except Exception:
            day = ""
    return f"{_norm(getattr(v, 'home', ''))}|{_norm(getattr(v, 'away', ''))}|{b['subkey']}|{day}"


def stake_for(cfg, fair_p, odds):
    """Javasolt tét (Ft) és a tőke hány %-a – ugyanúgy mint a webes felület.

    tét = bankroll * Kelly-tört * kelly_fraction (negyed Kelly), 100-ra kerekítve,
    de legalább min_bet. A % a bankroll-hoz viszonyított arány.
    """
    bankroll = float(cfg.get("live", {}).get("bankroll", 0))
    mult = float(cfg.get("value", {}).get("kelly_fraction", 0.25))
    min_bet = float(cfg.get("live", {}).get("min_bet", 100))
    frac = V.kelly_fraction(fair_p, odds) * mult
    pct = frac * 100.0
    st = _round_stake(bankroll * frac)
    if 0 < st < min_bet:
        st = int(min_bet)
    return st, pct


def _to_email(cfg):
    n = cfg.get("notify", {})
    return n.get("to_email") or n.get("smtp_user") or ""


def bet_dict(cfg, v, b):
    """A tipp egységes adat-dictje (emailhez + a 'Megraktam' tokenhez)."""
    st, pct = stake_for(cfg, b["fair_p"], b["odds"])
    key = b.get("key") or f"{v.sport_id}:{getattr(v, 'id', '')}:{b.get('subkey', '')}"
    start = v.start.isoformat() if getattr(v, "start", None) else b.get("start")
    return {
        "key": key,
        "sport": SPORT_NAMES.get(v.sport_id, ""),
        "event": f"{v.home} - {v.away}",
        "market": b.get("market", ""),
        "market_name": b["market_name"],
        "tip": b["tip"],
        "odds": round(float(b["odds"]), 3),
        "stake": int(st),
        "stake_pct": round(pct, 1),
        "value_pct": b["value_pct"],
        "fair_pct": b.get("fair_pct", round(b["fair_p"] * 100, 1)),
        "start": start,
        "limit": b.get("limit", 0),
        "pinn_url": b.get("pinn_url", ""),
    }


def format_bet(cfg, v, b):
    """Egy value bet email-sora, a javasolt téttel és a 'Megraktam' linkkel."""
    d = bet_dict(cfg, v, b)
    to = _to_email(cfg)
    stake_str = f"{d['stake']:,}".replace(",", " ")  # ezres tagolás szóközzel
    return (
        f"• {d['sport']} | {d['event']}\n"
        f"  {d['market_name']} – {d['tip']}\n"
        f"  Vegas odds {d['odds']:.2f} | value +{d['value_pct']}%"
        f" | Pinnacle limit ${d['limit']}\n"
        f"  💰 Javasolt tét: {stake_str} Ft  (a tőke {d['stake_pct']}%-a)\n"
        f"  Ellenőrzés: {d['pinn_url']}\n"
        f"  ✅ MEGRAKTAM:  {bettoken.placed_mailto(to, d)}\n"
        f"  ❌ Kihagytam:  {bettoken.skip_mailto(to, d)}\n")


def format_bet_html(cfg, v, b):
    """Egy tipp HTML-kártyája kattintható 'Megraktam / Kihagytam' gombokkal."""
    d = bet_dict(cfg, v, b)
    to = _to_email(cfg)
    stake_str = f"{d['stake']:,}".replace(",", " ")
    placed = bettoken.placed_mailto(to, d)
    skip = bettoken.skip_mailto(to, d)
    pinn = (f'<a href="{d["pinn_url"]}" style="color:#888">Pinnacle ellenőrzés</a>'
            if d["pinn_url"] else "")
    return f"""
    <div style="border:1px solid #e0e0e0;border-radius:10px;padding:12px 14px;margin:12px 0;font-family:Arial,Helvetica,sans-serif">
      <div style="font-size:12px;color:#888">{d['sport']}</div>
      <div style="font-size:16px;font-weight:bold;color:#111">{d['event']}</div>
      <div style="margin:4px 0;color:#222">{d['market_name']} – <b>{d['tip']}</b></div>
      <div style="font-size:14px;color:#222">Vegas odds <b>{d['odds']:.2f}</b> &middot; value <b style="color:#16a34a">+{d['value_pct']}%</b> &middot; limit ${d['limit']}</div>
      <div style="margin:4px 0;font-size:14px;color:#222">&#128176; Javasolt tét: <b>{stake_str} Ft</b> (a tőke {d['stake_pct']}%-a)</div>
      <div style="margin:12px 0 4px">
        <a href="{placed}" style="display:inline-block;background:#16a34a;color:#fff;text-decoration:none;padding:11px 18px;border-radius:8px;font-weight:bold">&#9989; Megraktam</a>
        &nbsp;&nbsp;
        <a href="{skip}" style="display:inline-block;background:#eceff1;color:#111;text-decoration:none;padding:11px 18px;border-radius:8px">&#10060; Kihagytam</a>
      </div>
      <div style="font-size:12px;margin-top:6px">{pinn}</div>
    </div>"""


def inject_env(cfg):
    """SMTP és Telegram belépési adatok a környezeti változókból (GitHub secrets)."""
    n = cfg.setdefault("notify", {})
    n["smtp_user"] = os.environ.get("SMTP_USER", n.get("smtp_user", ""))
    n["smtp_password"] = os.environ.get("SMTP_PASSWORD", n.get("smtp_password", ""))
    n["to_email"] = os.environ.get("TO_EMAIL", n.get("to_email") or n["smtp_user"])
    t = cfg.setdefault("telegram", {})
    t["token"] = os.environ.get("TELEGRAM_TOKEN", t.get("token", ""))
    t["chat_id"] = os.environ.get("TELEGRAM_CHAT_ID", t.get("chat_id", ""))
    return cfg


def send_telegram(notifier, cfg, items):
    """Tippenként egy Telegram-üzenet, ✅ Megraktam / ❌ Kihagytam gombokkal.

    Ha egyetlen üzenet sem ment ki (mind hibázott), hibát dob, hogy a hívó
    újrapróbálhassa (ne jelölje a tippeket 'ismertnek' küldés nélkül)."""
    sent = 0
    last_err = None
    for v, b in items:
        d = bet_dict(cfg, v, b)
        token = bettoken.token_block(d)
        try:
            notifier.send(format_tip(d, token), BUTTONS)
            sent += 1
        except Exception as e:
            last_err = e
            print(f"[telegram] HIBA: {e}")
    if items and sent == 0:
        raise RuntimeError(f"Telegram küldés sikertelen: {last_err}")
    return sent


def build_email(cfg, items, intro):
    """(text, html) páros a tippekből. items: (v, b) lista."""
    text = [intro] + [format_bet(cfg, v, b) for v, b in items]
    html_cards = "".join(format_bet_html(cfg, v, b) for v, b in items)
    html = (
        '<div style="max-width:580px;margin:0 auto">'
        f'<p style="font-family:Arial,Helvetica,sans-serif;font-size:15px;color:#111">{intro}</p>'
        f'{html_cards}'
        '<p style="font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#888">'
        'A &#9989; Megraktam gomb egy válasz-emailt nyit egy kóddal – csak küldd el. '
        'A figyelő ebből jegyzi meg a fogadást és követi az eredményt.</p>'
        '</div>')
    return "\n".join(text), html


def scan(cfg):
    http = Http(verify_ssl=cfg.get("http", {}).get("verify_ssl", True), delay_sec=0)
    vegas = VegasClient(http, cfg["vegas"])
    pinn = PinnacleClient(http)

    live = cfg.get("live", {})
    solid = live.get("solid", {})
    mcfg = cfg.get("matching", {})
    vcfg = cfg.get("value", {})
    devig = cfg.get("reference", {}).get("devig_method", "proportional")
    min_value = cfg.get("notify", {}).get("min_value_pct", 3.0)
    now = datetime.now(timezone.utc).timestamp()

    found = []
    for sid in live.get("sports", [66, 68, 67, 70]):
        ps = SPORT_MAP.get(sid)
        if not ps:
            continue
        try:
            ve, re_ = vegas.fetch_sport(sid), pinn.fetch_sport(ps)
        except Exception as e:
            print(f"  [{sid}] hiba: {e}")
            continue
        pairs = matching.match_events(ve, re_, mcfg.get("max_start_diff_minutes", 90),
                                      mcfg.get("min_token_score", 0.6))
        for v, r, sw, score in pairs:
            if score < solid.get("min_score", 0.8):
                continue
            for b in compute.compute_bets(v, r, sw, devig):
                val = b["value_pct"]
                if val < min_value or val > solid.get("max_value_pct", 20.0):
                    continue
                if b["odds"] < vcfg.get("min_odds", 1.2) or b["odds"] > solid.get("max_odds", 5.0):
                    continue
                if b.get("limit", 0) < solid.get("min_limit", 0):
                    continue
                mh = solid.get("max_hours_to_start", 0)
                if mh > 0:
                    if not v.start:
                        continue
                    hrs = (v.start.timestamp() - now) / 3600.0
                    if hrs < 0 or hrs > mh:
                        continue
                found.append((dedup_key(v, b), v, b))
    return found, now


def main():
    # Felhőben (publikus repo) csak config.example.json van; lokálisan config.json.
    path = "config.json" if os.path.exists("config.json") else "config.example.json"
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    inject_env(cfg)

    email = EmailNotifier(cfg)
    tg = TelegramNotifier(cfg)
    if not (email.configured() or tg.configured()):
        print("HIBA: sem email (SMTP_*), sem Telegram (TELEGRAM_*) nincs beállítva.")
        return

    found, now = scan(cfg)

    state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = {}
    state = {k: t for k, t in state.items() if now - t < KEEP_SEC}

    new = [(k, v, b) for (k, v, b) in found if k not in state]
    for k, _, _ in found:
        state[k] = now
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)

    if not new:
        print(f"Nincs új tipp ({len(found)} biztos, mind ismert).")
        return

    new.sort(key=lambda x: -x[2]["value_pct"])
    items = [(v, b) for _, v, b in new]
    if not tip_push_enabled():
        print(f"Per-tipp értesítés KI (TIP_PUSH!=1); {len(new)} új tipp nem lett kiküldve.")
        return
    if tg.configured():
        try:
            n_tg = send_telegram(tg, cfg, items)
            print(f"Telegram: {n_tg} tipp elküldve.")
        except Exception as e:
            print(f"Telegram küldés hiba: {e}")
    if cfg.get("notify", {}).get("enabled") and email.configured():
        try:
            text, html = build_email(cfg, items, f"{len(new)} új biztos value tipp a vegas.hu-n:")
            email.send(f"🟢 {len(new)} új value tipp – legjobb +{new[0][2]['value_pct']}%",
                       text, html)
            print(f"Email: {len(new)} új tipp elküldve.")
        except Exception as e:
            print(f"Email küldés hiba: {e}")


if __name__ == "__main__":
    main()
