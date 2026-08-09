"""Referencia-forrasok PARHUZAMOS osszehasonlitasa (dry-run, nem fogad semmit).

Miert kell: 2026-07-21-en a fair-forras csendben lecserelodott (Pinnacle 503 ->
Smarkets+Kambi), es hetekig senki nem vette eszre, hogy a value-t rossz vonalhoz
merjuk. A jelentesbol ez nem latszott (a CLV hibas volt, a yield-esest pedig
szorasnak lehetett nezni). Ez a szkript teszi lathatova, MIELOTT elesben futna:

  - ugyanarra a vegas.hu kinalatra kiszamolja a value beteket forrasonkent,
  - megmutatja, hany tippet ad mindegyik es mennyire ertenek egyet,
  - a KOZOS meccseken megmutatja, mennyire ternek el a fair valoszinusegek.

Ha ket forras ugyanarra a meccsre 3-4 szazalekponttal mast mond, akkor legfeljebb
az egyikuk lehet fair vonal -- es a kulonbseg nagysagrendje megmondja, mennyire
illuzio a 3-5%-os "edge".

Hasznalat:
    python compare_refs.py                 # pinnacle vs smarkets (alap)
    python compare_refs.py pinnacle kambi  # barmely ket forras
"""
import json
import sys

from valuebet.http import Http
from valuebet.vegas import VegasClient, SPORT_NAMES
from valuebet.reference import make_reference
from valuebet import matching, compute


def load_cfg():
    with open("config.json", encoding="utf-8") as f:
        return json.load(f)


def bets_for(cfg, source, http, vegas_cache):
    """Egy forrassal kiszamolt osszes fogadas: dedup-kulcs -> (bet, fair_p)."""
    c = json.loads(json.dumps(cfg))
    # egyetlen forras, szigoruan; a soft-tiltast is feloldjuk, hogy a Kambit is
    # OSSZE lehessen HASONLITANI (hasznalni tovabbra sem szabad fair vonalnak)
    c["reference"] = dict(c.get("reference", {}), provider="multi", sources=[source],
                          allow_soft=True, strict=False)
    ref = make_reference(http, c)
    mcfg = c.get("matching", {})
    devig = c.get("reference", {}).get("devig_method", "proportional")
    solid = c.get("live", {}).get("solid", {})
    out, ref_events = {}, 0
    for sid in c.get("live", {}).get("sports", [66, 68, 67, 70]):
        try:
            re_ = ref.fetch_for_vegas(sid)
        except Exception as e:
            print(f"  [{SPORT_NAMES.get(sid, sid)}] {source}: HIBA {str(e)[:80]}")
            continue
        if not re_:
            continue
        ref_events += len(re_)
        ve = vegas_cache.get(sid)
        if ve is None:
            continue
        pairs = matching.match_events(ve, re_, mcfg.get("max_start_diff_minutes", 90),
                                      mcfg.get("min_token_score", 0.6))
        for v, r, sw, score in pairs:
            if score < solid.get("min_score", 0.8):
                continue
            for b in compute.compute_bets(v, r, sw, devig):
                key = f"{v.home} - {v.away}|{b.get('subkey', '')}"
                out[key] = b
    return out, ref_events


def main():
    a = sys.argv[1] if len(sys.argv) > 1 else "pinnacle"
    b = sys.argv[2] if len(sys.argv) > 2 else "smarkets"
    cfg = load_cfg()
    http = Http(verify_ssl=cfg.get("http", {}).get("verify_ssl", True), delay_sec=0.2)
    vegas = VegasClient(http, cfg["vegas"])
    vegas_cache = {}
    for sid in cfg.get("live", {}).get("sports", [66, 68, 67, 70]):
        try:
            vegas_cache[sid] = vegas.fetch_sport(sid)
        except Exception as e:
            print(f"  vegas [{sid}]: HIBA {str(e)[:80]}")

    min_value = cfg.get("notify", {}).get("min_value_pct", 3.0)
    max_value = cfg.get("live", {}).get("solid", {}).get("max_value_pct", 20.0)
    res = {}
    for name in (a, b):
        bets, n_ev = bets_for(cfg, name, http, vegas_cache)
        vb = {k: x for k, x in bets.items() if min_value <= x["value_pct"] <= max_value}
        res[name] = (bets, vb, n_ev)
        print(f"{name:>10}: {n_ev:>4} referencia-esemeny, {len(bets):>4} parositott "
              f"fogadas, {len(vb):>3} value bet ({min_value}-{max_value}%)")

    (ba, va, _), (bb, vb_, _) = res[a], res[b]
    common = set(ba) & set(bb)
    print(f"\nKOZOS parositott fogadas: {len(common)}")
    if common:
        d = sorted(abs(ba[k]["fair_p"] - bb[k]["fair_p"]) * 100 for k in common)
        med = d[len(d) // 2]
        print(f"  fair valoszinuseg-elteres: median {med:.2f}pp, "
              f"atlag {sum(d) / len(d):.2f}pp, "
              f">3pp: {100 * sum(1 for x in d if x > 3) / len(d):.0f}%")
        print(f"  (viszonyitas: 1.70-es oddson egy 3%-os edge = 1,8pp valoszinuseg)")
    only_a = set(va) - set(vb_)
    only_b = set(vb_) - set(va)
    both = set(va) & set(vb_)
    print(f"\nvalue betek egyetertese: mindketto {len(both)}, csak {a} {len(only_a)}, "
          f"csak {b} {len(only_b)}")
    for k in sorted(both)[:5]:
        print(f"  = {k[:60]:<62} {a} {va[k]['value_pct']:>5.2f}% | "
              f"{b} {vb_[k]['value_pct']:>5.2f}%")
    for k in sorted(only_b)[:5]:
        other = ba.get(k)
        note = f"{a} szerint {other['value_pct']:+.2f}%" if other else f"{a} nem ismeri"
        print(f"  ! csak {b}: {k[:50]:<52} {vb_[k]['value_pct']:>5.2f}%  ({note})")
    print("\nOlvasat: a 'csak X' tippek azok, amiket a masik forras NEM tart value-nak."
          "\nHa ezek vannak tobbsegben, akkor a 'value' nagyresze forras-kulonbseg, nem el.")


if __name__ == "__main__":
    main()
