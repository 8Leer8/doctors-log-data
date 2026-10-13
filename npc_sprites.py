#!/usr/bin/env python3
"""
Builds npc_sprites.json: speaker name -> sprite base id (e.g. avg_npc_1612_1, char_013_riop).

v3 changes
  * Slot-aware stage model for BOTH script formats ([Character(name,name2,focus)] and
    [charslot(slot,name,focus)]), with focus persisting until changed.
  * No "npc"-only filter: story characters often use char_*/avg_<operator> sprites
    (e.g. "Heavily-Armed Man" = char_013_riop, "Federico" = avg_1032_excu2).
  * Local script cache (re-runs are fast / offline) and sprite-file validation.

Usage
    python npc_sprites.py --operators character_sprites.json
Options
    --cache DIR        script cache folder (default story_cache)
    --no-validate      skip checking that sprite files exist in the image repo
"""
import argparse, json, re, sys, os, time, urllib.parse, urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

EXCEL = "https://cdn.jsdelivr.net/gh/ArknightsAssets/ArknightsGamedata@master/en/gamedata/excel"
STORY = "https://cdn.jsdelivr.net/gh/ArknightsAssets/ArknightsGamedata@master/en/gamedata/story"
STORY_FALLBACK = "https://cdn.jsdelivr.net/gh/Kengxxiao/ArknightsGameData_YoStar@main/en_US/gamedata/story"
IMG_RAW = "https://raw.githubusercontent.com/akgcc/arkdata/main/assets/avg/characters"

CHAR_TAG = re.compile(r"^\[character(?:\(|\])", re.I)
SLOT_TAG = re.compile(r"^\[charslot(?:\(|\])", re.I)
NAME1 = re.compile(r'(?<![A-Za-z0-9])name\s*=\s*"([^"]*)"')
NAME2 = re.compile(r'(?<![A-Za-z0-9])name2\s*=\s*"([^"]*)"')
FOCUS_NUM = re.compile(r"focus\s*=\s*(-?\d)")
SLOT = re.compile(r'(?<![A-Za-z0-9])slot\s*=\s*"([^"]*)"', re.I)
FOCUS_STR = re.compile(r'(?<![A-Za-z0-9])focus\s*=\s*"?([A-Za-z]*)"?', re.I)
SPEAKER = re.compile(r'^\[name="([^"]*)"\]\s*(.*)$')
SLOT_MAP = {"l": "L", "left": "L", "m": "M", "middle": "M", "r": "R", "right": "R"}

VARIANT_SUFFIX = re.compile(r"(#\d+)?(\$\d+)?$")
SKIP_SPEAKER = re.compile(r"^doctor$|voice|\?\?|^\W*$| & | and |\?$", re.I)


def http_get(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "npc-sprite-builder"})
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status == 200:
                    return r.read().decode("utf-8", errors="replace")
        except Exception:
            time.sleep(0.5 * (i + 1))
    return None


def clean(v):
    if v is None:
        return None
    v = v.strip().lower()
    return None if v in ("", "char_empty") else v


def normalize(sprite):
    return VARIANT_SUFFIX.sub("", sprite)


def _legacy_focus(v):
    if v == 1: return {"A"}
    if v == 2: return {"B"}
    if v == -1: return set()      # nobody on stage is speaking: the speaker is off-screen
    return None                   # 0 / 3: default highlight


def replay_stage(raw):
    """Yield (speaker, stage{slot: sprite}, focus) for every dialogue line.

    focus: None    = default / unknown
           set()   = explicitly nobody (speaker is off-screen: phone call, radio, out of frame)
           {slots} = the highlighted slot(s)
    """
    stage, focus = {}, None
    for rl in raw.split("\n"):
        line = rl.strip()
        if not line:
            continue

        if CHAR_TAG.match(line):                      # legacy: name / name2 / focus=1|2|-1
            a, b, f = NAME1.search(line), NAME2.search(line), FOCUS_NUM.search(line)
            if a is None and b is None:
                if f:
                    focus = _legacy_focus(int(f.group(1)))
                else:
                    stage, focus = {}, None
                continue
            if a:
                v = clean(a.group(1))
                stage.pop("A", None)
                if v: stage["A"] = v
            if b:
                v = clean(b.group(1))
                stage.pop("B", None)
                if v: stage["B"] = v
            elif a:
                stage.pop("B", None)
            focus = _legacy_focus(int(f.group(1))) if f else None
            continue

        if SLOT_TAG.match(line):                      # newer: slot l/m/r + focus="l|m|r|n|all"
            sm, nm, fm = SLOT.search(line), NAME1.search(line), FOCUS_STR.search(line)
            if sm is None and nm is None and fm is None:
                stage, focus = {}, None               # bare tag / duration-only: clear stage
                continue
            if sm and nm:
                slot = SLOT_MAP.get(sm.group(1).lower())
                if slot:
                    v = clean(nm.group(1))
                    if v: stage[slot] = v
                    else: stage.pop(slot, None)
            if fm and "focus" in line.lower():
                fv = fm.group(1).lower()
                if fv in SLOT_MAP: focus = {SLOT_MAP[fv]}
                elif fv in ("all", "a"): focus = None
                else: focus = set()                   # n / none: nobody speaking
            elif nm:
                focus = None                          # new sprite without focus: default
            continue

        m = SPEAKER.match(line)
        if m and m.group(2).strip():
            yield (m.group(1).strip() or None, dict(stage), None if focus is None else set(focus))


def credit(stage, focus):
    """Which sprites to credit for this line, with weights."""
    if focus is not None and len(focus) == 0:
        return []                                     # speaker is off-screen: stage sprites are not theirs
    fs = [stage[k] for k in (focus or ()) if k in stage]
    if len(fs) == 1:
        return [(fs[0], 4.0)]                         # focused slot: strong
    if len(stage) == 1:
        return [(next(iter(stage.values())), 3.0)]    # only sprite on stage
    return [(s, 1.0) for s in stage.values()]         # several, no focus: weak


def scan_script(raw, operators, counts):
    for speaker, stage, focus in replay_stage(raw):
        if not speaker or speaker in operators or SKIP_SPEAKER.search(speaker):
            continue
        for sprite, w in credit(stage, focus):
            counts[speaker][normalize(sprite)] += w


# ---- port of AssetRef._buildFilenameVariants (Dart), used for validation ----
def is_npc(i):
    return i.startswith(("avg_npc", "avgnew_npc", "npc_"))


def variants(raw_id):
    out, seen = [], set()
    def add(n):
        if n not in seen: seen.add(n); out.append(n)
    def all_for(b):
        if re.search(r"#\d+\$\d+$", b): add(b + ".png"); return
        if re.search(r"#\d+$", b): add(b + "$1.png"); return
        if is_npc(b): add(b + "#1$1.png")
        m = re.match(r"^(.+_\d+)_\d+$", b)
        if m: add(m.group(1) + "#1$1.png"); add(m.group(1) + "_1#1$1.png")
        add(b + "#1$1.png"); add(b + "_1#1$1.png")
    def add_for(b):
        all_for(b)
        if is_npc(b): return
        if b.startswith("char_"): all_for("avg_" + b[5:])
        elif b.startswith("avg_"): all_for("char_" + b[4:])
    low = raw_id.lower(); wo = low.replace("_ex", "")
    if wo != low: add_for(wo)
    add_for(low)
    return out


def exists(filename):
    url = f"{IMG_RAW}/{urllib.parse.quote(filename)}"
    for _ in range(3):
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "npc-sprite-builder"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status == 200
        except urllib.error.HTTPError as e:
            if e.code == 404: return False
            time.sleep(0.5)
        except Exception:
            time.sleep(0.5)
    return False


def resolves(sprite_id):
    return any(exists(v) for v in variants(sprite_id))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--operators", required=True)
    ap.add_argument("--out", default="npc_sprites.json")
    ap.add_argument("--ambiguous", default="npc_ambiguous.json")
    ap.add_argument("--missing", default="npc_missing.json")
    ap.add_argument("--cache", default="story_cache")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--min-score", type=float, default=6.0)
    ap.add_argument("--min-share", type=float, default=0.6)
    ap.add_argument("--strong-score", type=float, default=150.0)
    ap.add_argument("--strong-share", type=float, default=0.4)
    ap.add_argument("--no-validate", action="store_true")
    args = ap.parse_args()

    try:
        with open(args.operators, encoding="utf-8") as f:
            operators = set(json.load(f).keys())
    except FileNotFoundError:
        sys.exit(f"Operators file not found: {args.operators}")

    os.makedirs(args.cache, exist_ok=True)
    idx_path = os.path.join(args.cache, "_index.json")
    raw_index = open(idx_path, encoding="utf-8").read() if os.path.exists(idx_path) else http_get(f"{EXCEL}/story_review_table.json")
    if raw_index is None:
        sys.exit("Could not fetch story_review_table.json")
    open(idx_path, "w", encoding="utf-8").write(raw_index)
    index = json.loads(raw_index)

    files = sorted({(u.get("storyTxt") or "") + ".txt" for e in index.values()
                    for u in (e.get("infoUnlockDatas") or []) if u.get("storyTxt")})
    print(f"Scanning {len(files)} scripts (cache: {args.cache})...")

    def fetch(f):
        path = os.path.join(args.cache, f.replace("/", "__"))
        if os.path.exists(path):
            return f, open(path, encoding="utf-8", errors="replace").read()
        raw = http_get(f"{STORY}/{f}") or http_get(f"{STORY_FALLBACK}/{f}")
        if raw is not None:
            open(path, "w", encoding="utf-8").write(raw)
        return f, raw

    counts, failed = defaultdict(lambda: defaultdict(float)), []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, (f, raw) in enumerate(pool.map(fetch, files), 1):
            if raw is None: failed.append(f)
            else: scan_script(raw, operators, counts)
            if i % 200 == 0: print(f"  {i}/{len(files)}")

    result, ambiguous = {}, {}
    for speaker, ids in counts.items():
        ranked = sorted(ids.items(), key=lambda kv: kv[1], reverse=True)
        total = sum(v for _, v in ranked)
        top_id, top_score = ranked[0]
        if top_score < args.min_score: continue
        share = top_score / total
        if share >= args.min_share or (top_score >= args.strong_score and share >= args.strong_share):
            result[speaker] = top_id
        else:
            ambiguous[speaker] = {k: v for k, v in ranked[:3]}

    missing = {}
    if not args.no_validate:
        uniq = sorted(set(result.values()))
        print(f"Validating {len(uniq)} sprite ids against the image repo...")
        with ThreadPoolExecutor(max_workers=16) as pool:
            ok = dict(zip(uniq, pool.map(resolves, uniq)))
        for sp in list(result):
            if not ok[result[sp]]:
                missing[sp] = result.pop(sp)

    dump = lambda p, d: json.dump(dict(sorted(d.items())), open(p, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    dump(args.out, result); dump(args.ambiguous, ambiguous); dump(args.missing, missing)
    print(f"Wrote {len(result)} speakers to {args.out}")
    print(f"{len(ambiguous)} ambiguous -> {args.ambiguous}; {len(missing)} had no image file -> {args.missing}")
    if failed: print(f"{len(failed)} scripts unavailable: {failed[:3]}...")


if __name__ == "__main__":
    main()