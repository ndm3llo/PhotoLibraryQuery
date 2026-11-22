"""Measure face matching on a labeled folder of photos.

    python scripts/eval_faces.py                  # uses app_imgs/ (gitignored)
    python scripts/eval_faces.py --dir ~/faces --check

Layout:  <dir>/<person>/*.jpg  and  <dir>/others/**/*.jpg  (strangers: people who are NOT any of the named ones).
Strangers may be grouped in subfolders, e.g. others/looks_like_ari/. The report then shows, per group, how close
the strangers come to the person they resemble (family look-alikes are the hardest case for any face model).

Ground truth: a photo in a person's folder that contains exactly ONE confident face identifies that face
(group photos are ambiguous, so they are not used as truth). Every face in `others/` must match nobody.
Each known face is then looked up against all the OTHER known faces (leave-one-out; the identical image is never
evidence), and we report how often the nearest face is the right person, a WRONG known person, or too far to
call a match, plus how many strangers falsely match. Faces are embedded exactly as the worker does it
(detected on a downscaled copy, embedded from the full-resolution original).

Use it to choose and re-check the cutoffs in app/config.py (FACE_AUTO_MAX / FACE_SUGGEST_MAX), and to compare any
new face model. Results are cached in .cache/ so re-runs with the same photos and model are instant.

--check exits non-zero if the configured auto cutoff makes ANY wrong or stranger match on this set.
"""
import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import FACE_AUTO_MAX, FACE_SUGGEST_MAX  # noqa: E402
from app.services import faces, imaging  # noqa: E402

EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
CONFIDENT = 0.6  # a detection this sure counts when deciding "exactly one face in the photo"
CACHE = Path(".cache/eval_faces.json")


def embed_all(root: Path) -> list[dict]:
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    engine, out, todo = None, [], []
    files = [p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower() in EXT]

    def labels(p: Path) -> tuple[str, str | None]:
        parts = p.relative_to(root).parts  # (person, file) or (others, group, ..., file)
        return parts[0], (parts[1] if parts[0] == "others" and len(parts) > 2 else None)

    for p in files:
        key = hashlib.sha1(f"{p.resolve()}|{p.stat().st_mtime_ns}|{p.stat().st_size}|{faces.MODEL_VERSION}".encode()).hexdigest()
        if key in cache:
            person, group = labels(p)
            out.append({**cache[key], "person": person, "group": group})
        else:
            todo.append((p, key))
    if todo:
        print(f"embedding {len(todo)} new photo(s) ({len(files) - len(todo)} cached)...", file=sys.stderr)
        engine = faces.get_engine()
        for i, (p, key) in enumerate(todo, 1):
            person, group = labels(p)
            rec = {"person": person, "group": group, "file": p.name, "faces": []}
            try:
                for f in engine.detect(imaging.open_image(p.read_bytes())):
                    rec["faces"].append({"det": f.det_score, "px": f.px, "emb": f.embedding})
            except Exception as e:
                rec["error"] = repr(e)[:100]
            cache[key] = rec
            out.append(rec)
            if i % 25 == 0:
                print(f"  {i}/{len(todo)}", file=sys.stderr)
        CACHE.parent.mkdir(exist_ok=True)
        CACHE.write_text(json.dumps(cache))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=Path("app_imgs"))
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    recs = embed_all(args.dir)
    known, strangers, errors = [], [], 0
    for r in recs:
        if "error" in r:
            errors += 1
            continue
        good = [f for f in r["faces"] if f["det"] >= CONFIDENT]
        if r["person"] == "others":
            strangers += [{"emb": np.array(f["emb"], dtype=np.float32), "px": f["px"], "group": r.get("group") or "(unsorted)"}
                          for f in good]
        elif len(good) == 1:
            known.append({"person": r["person"], "emb": np.array(good[0]["emb"], dtype=np.float32), "px": good[0]["px"]})

    counts = Counter(k["person"] for k in known)
    print(f"model {faces.MODEL_VERSION} | {len(recs)} photos ({errors} unreadable) | known faces {dict(sorted(counts.items()))} | "
          f"{len(strangers)} stranger faces")
    thin = [p for p, n in counts.items() if n < 2]
    if thin:
        print(f"NOTE: {thin} have fewer than 2 one-face photos, so they cannot be measured (nothing to match them against).")
    if len(known) < 5 or not strangers:
        print("Not enough labeled data to evaluate.")
        return 2

    G = np.stack([k["emb"] for k in known])
    labels = np.array([k["person"] for k in known])

    def nearest(q, skip=None):
        d = 1 - G @ q
        d[d < 0.02] = np.inf
        if skip is not None:
            d[skip] = np.inf
        j = int(np.argmin(d))
        return j, float(d[j])

    rows = []
    for i, k in enumerate(known):
        j, d = nearest(k["emb"], i)
        if np.isfinite(d):
            rows.append((k["person"], labels[j], d, k["px"]))
    snear = [nearest(s["emb"]) for s in strangers]
    sdist = [d for _, d in snear]
    n = len(rows)

    same = [r[2] for r in rows if r[0] == r[1]]
    print(f"closed-set accuracy {100 * sum(r[0] == r[1] for r in rows) / n:.0f}% of {n} | same-person distance: median "
          f"{np.median(same):.2f}, 90th pct {np.percentile(same, 90):.2f} | nearest stranger: min {min(sdist):.2f}, median {np.median(sdist):.2f}")
    print(f"\n{'max distance':>13} | {'right':>6} {'WRONG known':>12} {'no match':>9} | {'strangers matched':>18}")
    cut_stats = {}
    for t in sorted({0.30, 0.40, 0.50, 0.60, 0.70, FACE_AUTO_MAX, FACE_SUGGEST_MAX}):
        ok = sum(r[0] == r[1] and r[2] < t for r in rows) / n
        bad = sum(r[0] != r[1] and r[2] < t for r in rows) / n
        miss = sum(r[2] >= t for r in rows) / n
        fm = sum(d < t for d in sdist) / len(sdist)
        mark = "  <- FACE_AUTO_MAX" if t == FACE_AUTO_MAX else "  <- FACE_SUGGEST_MAX" if t == FACE_SUGGEST_MAX else ""
        print(f"{t:>13.2f} | {100 * ok:>5.0f}% {100 * bad:>11.0f}% {100 * miss:>8.0f}% | {100 * fm:>17.0f}%{mark}")
        cut_stats[t] = (bad, fm)

    grouped = defaultdict(list)
    for s_, (j, d) in zip(strangers, snear):
        grouped[s_["group"]].append((d, labels[j]))
    if len(grouped) > 1 or "(unsorted)" not in grouped:
        print("\nstrangers by group (closest known face; look-alikes are the hard case):")
        for g, items in sorted(grouped.items()):
            dmin, who = min(items)
            auto = sum(d < FACE_AUTO_MAX for d, _ in items)
            sug = sum(d < FACE_SUGGEST_MAX for d, _ in items)
            print(f"  {g:22} {len(items):>3} faces | closest {dmin:.2f} (to {who}) | falsely matched: {auto} at auto cutoff, {sug} at suggest cutoff")

    per = defaultdict(lambda: [0, 0, Counter()])
    for who, pred, _, _ in rows:
        per[who][0] += 1
        per[who][1] += who == pred
        if who != pred:
            per[who][2][pred] += 1
    print("\nper person:")
    for who, (t, ok, conf) in sorted(per.items()):
        print(f"  {who:12} {ok:>3}/{t:<3} right" + (f"   confused with {dict(conf.most_common(2))}" if conf else ""))
    print("\nby face size (original px):")
    for lo, hi, name in [(0, 60, "under 60"), (60, 100, "60-100"), (100, 150, "100-150"), (150, 10**6, "150+")]:
        sub = [r for r in rows if lo <= r[3] < hi]
        if sub:
            print(f"  {name:10} {sum(r[0] == r[1] for r in sub):>3}/{len(sub):<3} right")

    if args.check:
        bad, fm = cut_stats[FACE_AUTO_MAX]
        if bad or fm:
            print(f"\nCHECK FAILED: FACE_AUTO_MAX={FACE_AUTO_MAX} makes {100 * bad:.0f}% wrong-person and {100 * fm:.0f}% stranger matches")
            return 1
        print(f"\nCHECK OK: no wrong-person or stranger match at FACE_AUTO_MAX={FACE_AUTO_MAX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
