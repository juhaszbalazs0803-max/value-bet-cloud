"""value-bet parancssori felület.

Használat:
    python -m valuebet --list           # csak vegas.hu meccsek + margin (kulcs nélkül)
    python -m valuebet                  # value betek keresése (referenciával)
"""
import argparse
import json
import os
import sys

from .http import Http
from .vegas import VegasClient, SPORT_NAMES
from .oddsapi import OddsApiClient
from . import matching, value as V


def load_config(path):
    if not os.path.exists(path):
        sys.exit(f"Nincs config fájl: {path}\n"
                 "Másold át a config.example.json-t config.json néven és töltsd ki.")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _print_table(rows, headers):
    if not rows:
        print("  (nincs találat)")
        return
    cols = list(zip(*([headers] + rows)))
    widths = [max(len(str(c)) for c in col) for col in cols]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)))


def fetch_vegas(http, cfg, only_sport=None):
    client = VegasClient(http, cfg["vegas"])
    sport_ids = cfg["vegas"]["sport_ids"]
    if only_sport is not None:
        sport_ids = [only_sport]
    events = []
    for sid in sport_ids:
        print(f"  vegas.hu: {SPORT_NAMES.get(sid, sid)} (sportId={sid}) ...", flush=True)
        try:
            evs = client.fetch_sport(sid)
            events.extend(evs)
            print(f"    -> {len(evs)} meccs")
        except Exception as e:
            print(f"    HIBA: {e}")
    return events


def cmd_list(http, cfg):
    events = fetch_vegas(http, cfg, only_sport=cfg.get("_only_sport"))
    events.sort(key=lambda e: (e.start is None, e.start))
    rows = []
    for e in events:
        odds_list = [e.odds[k] for k in ("home", "draw", "away") if k in e.odds]
        margin = V.margin_pct(odds_list)
        t = e.start.strftime("%m-%d %H:%M") if e.start else "?"
        o = "/".join(f"{e.odds.get(k):.2f}" for k in ("home", "draw", "away") if k in e.odds)
        rows.append([e.sport_name, t, f"{e.home} - {e.away}"[:40], o, f"{margin:.1f}%"])
    print(f"\nÖsszesen {len(rows)} meccs:\n")
    _print_table(rows, ["Sport", "Idő (UTC)", "Meccs", "Odds (1/X/2)", "Margin"])


def cmd_value(http, cfg):
    ref_cfg = cfg["reference"]
    if ref_cfg.get("provider", "pinnacle") == "pinnacle":
        return cmd_value_pinnacle(http, cfg)
    if "IDE_JON" in str(ref_cfg.get("oddsapi_key", "")):
        sys.exit("A The Odds API-hoz állítsd be a 'reference.oddsapi_key'-t a config.json-ban,\n"
                 "vagy használd a Pinnacle-t: 'reference.provider': 'pinnacle' (kulcs nélkül).")

    print("Referencia-odds letöltése (The Odds API) ...", flush=True)
    ref = OddsApiClient(http, ref_cfg).fetch_reference()
    print(f"  -> {len(ref)} referencia-meccs\n")

    print("vegas.hu odds letöltése ...", flush=True)
    vegas_events = fetch_vegas(http, cfg)
    print()

    mcfg = cfg["matching"]
    pairs = matching.match_events(
        vegas_events, ref,
        max_start_diff_min=mcfg.get("max_start_diff_minutes", 90),
        min_score=mcfg.get("min_token_score", 0.5),
    )
    print(f"Párosított meccsek: {len(pairs)}\n")

    vc = cfg["value"]
    min_v = vc.get("min_value_pct", 3.0)
    min_o, max_o = vc.get("min_odds", 1.2), vc.get("max_odds", 15.0)
    kf = vc.get("kelly_fraction", 0.25)
    swap = {"home": "away", "away": "home", "draw": "draw"}

    rows = []
    for ve, re_, swapped, _score in pairs:
        for label in ("home", "draw", "away"):
            odds = ve.odds.get(label)
            if not odds or odds < min_o or odds > max_o:
                continue
            ref_key = swap[label] if swapped else label
            fair_p = re_.fair.get(ref_key)
            if not fair_p:
                continue
            val = V.value_pct(fair_p, odds)
            if val < min_v:
                continue
            kelly = V.kelly_fraction(fair_p, odds) * kf * 100
            tip = {"home": f"1 {ve.home}", "draw": "X (döntetlen)", "away": f"2 {ve.away}"}[label]
            t = ve.start.strftime("%m-%d %H:%M") if ve.start else "?"
            rows.append([val, ve.sport_name, t, f"{ve.home} - {ve.away}"[:34],
                         tip[:24], f"{odds:.2f}", f"{fair_p*100:.1f}%",
                         f"+{val:.1f}%", f"{kelly:.1f}%", re_.n_books])

    rows.sort(key=lambda r: r[0], reverse=True)
    table = [r[1:] for r in rows]
    print(f"VALUE BETEK (value >= {min_v}%):\n")
    _print_table(table, ["Sport", "Idő(UTC)", "Meccs", "Tipp", "Odds",
                          "Valós", "Value", "Kelly", "#irodák"])
    print(f"\n{len(table)} value bet. (Kelly = javasolt tét a bankroll %-ában, "
          f"{kf:g}x Kelly-vel.)")


def cmd_value_pinnacle(http, cfg):
    """Egyszeri value-lista Pinnacle referenciával (a --web motorját futtatja egy ciklusra)."""
    from .engine import ValueEngine
    print("vegas.hu + Pinnacle letöltése, párosítás ...", flush=True)
    engine = ValueEngine(http, cfg)
    engine._cycle()
    snap = engine.snapshot()
    m = snap["meta"]
    print(f"  vegas: {m['vegas_events']} meccs | pinnacle: {m['pinn_events']} meccs | "
          f"párosítva: {m['matched']}\n")
    rows = [[b["sport"], (b["start"] or "?")[5:16].replace("T", " "),
             b["event"][:34], b["tip"][:24], f"{b['odds']:.2f}",
             f"{b['pinn_odds']:.2f}" if b["pinn_odds"] else "–",
             f"{b['fair_pct']}%", f"+{b['value_pct']}%", f"{b['kelly_pct']}%"]
            for b in snap["bets"]]
    s = snap["settings"]
    print(f"VALUE BETEK (value >= {s['min_value_pct']}%, odds {s['min_odds']}–{s['max_odds']}):\n")
    _print_table(rows, ["Sport", "Idő(UTC)", "Meccs", "Tipp", "Odds",
                        "Pinnacle", "Valós", "Value", "Kelly"])
    print(f"\n{len(rows)} value bet. Élő követéshez: python -m valuebet --web")


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    p = argparse.ArgumentParser(prog="valuebet", description="Value bet kereső a vegas.hu-ra")
    p.add_argument("--config", default="config.json", help="config fájl útvonala")
    p.add_argument("--list", action="store_true",
                   help="csak a vegas.hu meccseket és marginokat listázza (kulcs nélkül)")
    p.add_argument("--sport", type=int, help="csak ez a sportId (pl. 66=foci)")
    p.add_argument("--min-value", type=float, help="value%% küszöb felülírása")
    p.add_argument("--insecure", action="store_true", help="SSL-ellenőrzés kikapcsolása")
    p.add_argument("--web", action="store_true",
                   help="élő webes felület indítása (Pinnacle referenciával, auto-frissítés)")
    p.add_argument("--port", type=int, default=8765, help="webes felület portja")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    if args.sport is not None:
        cfg["_only_sport"] = args.sport
        cfg["vegas"]["sport_ids"] = [args.sport]
    if args.min_value is not None:
        cfg["value"]["min_value_pct"] = args.min_value

    hcfg = cfg.get("http", {})
    verify = False if args.insecure else hcfg.get("verify_ssl", True)
    http = Http(verify_ssl=verify, delay_sec=hcfg.get("request_delay_sec", 0.3))

    if args.web:
        from .engine import ValueEngine
        from .server import serve
        print("Élő value-motor indítása (vegas.hu + Pinnacle)...")
        engine = ValueEngine(http, cfg)
        serve(engine, port=args.port)
    elif args.list:
        cmd_list(http, cfg)
    else:
        cmd_value(http, cfg)


if __name__ == "__main__":
    main()
