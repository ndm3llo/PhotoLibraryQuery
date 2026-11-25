"""Who is in a photo: matching faces to named people, and grouping unnamed faces.

Two layers:
  * Pure functions (`match_face`, `cluster_faces`): no database, easy to test and to tune.
  * Database functions (`assign_library_faces`, `recluster_library`): apply them to one library.

Rules that protect the data:
  * A face a person labeled (`person_source` manual or confirmed) is GROUND TRUTH. Nothing here ever changes it.
  * A match this code makes is stored as `person_source = 'auto'` and is never treated as ground truth, so
    errors cannot feed on themselves. It is also the only kind of label that is re-computed.
  * Suggestions (between the auto and suggest cutoffs) are never stored and never pushed to anyone; they are
    computed when someone opens the "Who is this?" page.
  * People who opted out are never matched. A face someone rejected for a person is never offered for them again.
  * Faces are only compared with faces from the same model (`model_version`).

Matching tiers (cutoffs in app/config.py, chosen with scripts/eval_faces.py):
  auto     best distance <= FACE_AUTO_MAX and face quality >= FACE_AUTO_MIN_QUALITY
  suggest  best distance <= FACE_SUGGEST_MAX
  The best and runner-up persons are both reported so a margin rule ("clearly closer than the next person")
  can be added without changing callers; see docs/ARCHITECTURE.md (Phase 4, open question).

Scale: galleries are loaded into memory and compared with numpy. That is the right tool for one family
(tens of thousands of faces); revisit with pgvector queries only if a library grows far beyond that.
"""
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Literal
from uuid import UUID

import numpy as np
from sqlalchemy import text
from sqlmodel import Session

from app.config import FACE_AUTO_MAX, FACE_AUTO_MIN_QUALITY, FACE_SUGGEST_MAX

Tier = Literal["auto", "suggest"]
GROUND_TRUTH = ("manual", "confirmed")
_SELF_EPS = 0.02  # an embedding this close is the same image, not independent evidence


@dataclass(frozen=True)
class Match:
    person_id: UUID
    distance: float
    tier: Tier
    runner_up_distance: float | None  # nearest face of a DIFFERENT person, if any


def _vec(v) -> np.ndarray:
    """pgvector columns come back from raw SQL as text like '[0.1,0.2]'; accept that or a sequence."""
    if isinstance(v, str):
        return np.array(v.strip("[]").split(","), dtype=np.float32)
    return np.asarray(v, dtype=np.float32)


def _unit(v) -> np.ndarray:
    a = _vec(v)
    n = np.linalg.norm(a)
    return a / n if n else a


def match_face(
    embedding,
    quality: float,
    gallery: Iterable[tuple[UUID, object]],
    *,
    excluded: frozenset[UUID] = frozenset(),
    auto_max: float = FACE_AUTO_MAX,
    suggest_max: float = FACE_SUGGEST_MAX,
    min_quality: float = FACE_AUTO_MIN_QUALITY,
) -> Match | None:
    """Best person for one face, or None. `gallery` is (person_id, embedding) pairs of known faces/selfies.
    `excluded` persons (opted out, or rejected for this face) are never returned."""
    q = _unit(embedding)
    best: dict[UUID, float] = {}
    for pid, emb in gallery:
        if pid in excluded:
            continue
        d = 1.0 - float(q @ _unit(emb))
        if d < _SELF_EPS:
            continue
        if d < best.get(pid, 2.0):
            best[pid] = d
    if not best:
        return None
    ranked = sorted(best.items(), key=lambda kv: kv[1])
    pid, d = ranked[0]
    if d > suggest_max:
        return None
    tier: Tier = "auto" if d <= auto_max and quality >= min_quality else "suggest"
    return Match(pid, round(d, 4), tier, round(ranked[1][1], 4) if len(ranked) > 1 else None)


def cluster_faces(embeddings: list, max_distance: float = FACE_AUTO_MAX, order: list[float] | None = None,
                  link_max: float = FACE_SUGGEST_MAX) -> list[list[int]]:
    """Group faces that look like the same person. Greedy and deterministic: faces are visited best-first
    (highest `order` value, e.g. quality) and join the nearest cluster when BOTH hold:
      * the face is within `max_distance` of the cluster's centroid, and
      * it is within `link_max` of EVERY member already in the cluster.
    The second rule stops chaining: a centroid drifts toward each new member, so without it A, a look-alike B
    and a third person C (close to B, far from A) would all end up together.
    Returns clusters as lists of indexes into `embeddings`, largest first."""
    idx = sorted(range(len(embeddings)), key=lambda i: -(order[i] if order else 0.0))
    units = [_unit(e) for e in embeddings]
    centroids: list[np.ndarray] = []
    members: list[list[int]] = []

    def home(i: int) -> int | None:
        """The nearest cluster face `i` may join, or None."""
        if not centroids:
            return None
        d = 1.0 - np.stack(centroids) @ units[i]
        for j in np.argsort(d):
            if d[j] > max_distance:
                return None
            if 1.0 - float((np.stack([units[m] for m in members[j]]) @ units[i]).min()) <= link_max:
                return int(j)
        return None

    for i in idx:
        j = home(i)
        if j is None:
            members.append([i])
            centroids.append(units[i])
        else:
            members[j].append(i)
            centroids[j] = _unit(np.sum([units[m] for m in members[j]], axis=0))
    return sorted(members, key=len, reverse=True)


def fold(text_: str) -> str:
    """How names are compared when searching: ignores capitals, accents, and extra spaces, so "MARIA", "maria"
    and " María " are the same name (and "Jose" finds "José"). Display always keeps the original spelling."""
    decomposed = unicodedata.normalize("NFKD", text_)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.casefold().split())


def rank_names(query: str, candidates: list[dict], limit: int = 8) -> list[dict]:
    """Type-ahead over a library's people. `candidates` are dicts with display_name, qualifier, aliases.
    Ranked: exact name, name starts with the text, a nickname starts with it, any word of the name/qualifier
    starts with it. Capitals, accents and extra spaces are ignored (see `fold`). Adds `via_alias` when a nickname
    was what matched."""
    q = fold(query)
    if not q:
        return []
    scored = []
    for c in candidates:
        name = fold(c["display_name"])
        full = f"{name} {fold(c.get('qualifier') or '')}".strip()
        alias = next((a for a in sorted(c.get("aliases") or [], key=len) if fold(a).startswith(q)), None)
        if q in (name, full):
            rank, via = 0, None
        elif name.startswith(q) or full.startswith(q):
            rank, via = 1, None
        elif alias:
            rank, via = 2, alias
        elif any(w.startswith(q) for w in full.split()):
            rank, via = 3, None
        else:
            continue
        scored.append((rank, name, {**c, "via_alias": via}))
    return [c for _, _, c in sorted(scored, key=lambda t: (t[0], t[1]))[:limit]]


# ------------------------------------------------------------------------------------- database

def _gallery(session: Session, library_id: UUID, model: str) -> list[tuple[UUID, np.ndarray]]:
    rows = session.execute(text("""
        SELECT person_id, embedding FROM face
         WHERE library_id = :l AND model_version = :m AND person_id IS NOT NULL
           AND person_source IN ('manual', 'confirmed')
        UNION ALL
        SELECT person_id, embedding FROM person_reference WHERE library_id = :l AND model_version = :m"""),
        {"l": library_id, "m": model}).all()
    return [(r[0], _vec(r[1])) for r in rows]


def assign_library_faces(session: Session, library_id: UUID, model: str) -> dict[str, int]:
    """Re-compute the automatic labels for one library: clear old 'auto' labels, then label every face that has
    no person yet where the match is in the auto tier. Ground-truth labels are never touched. Idempotent."""
    session.execute(text("""UPDATE face SET person_id = NULL, person_source = NULL, person_confidence = NULL
                             WHERE library_id = :l AND person_source = 'auto'"""), {"l": library_id})
    opted_out = frozenset(r[0] for r in session.execute(
        text("SELECT id FROM person WHERE library_id = :l AND recognition_opt_out"), {"l": library_id}))
    rejected: dict[UUID, set[UUID]] = {}
    for fid, pid in session.execute(text("SELECT face_id, person_id FROM face_rejection WHERE library_id = :l"),
                                    {"l": library_id}):
        rejected.setdefault(fid, set()).add(pid)
    gallery = _gallery(session, library_id, model)
    todo = session.execute(text("""SELECT id, embedding, COALESCE(quality, 0) FROM face
                                    WHERE library_id = :l AND model_version = :m AND person_id IS NULL"""),
                           {"l": library_id, "m": model}).all()
    labeled = 0
    for fid, emb, quality in todo:
        m = match_face(emb, float(quality), gallery, excluded=opted_out | frozenset(rejected.get(fid, ())))
        if m and m.tier == "auto":
            session.execute(text("""UPDATE face SET person_id = :p, person_source = 'auto', person_confidence = :c
                                     WHERE id = :f"""), {"p": m.person_id, "c": round(1 - m.distance, 4), "f": fid})
            labeled += 1
    return {"considered": len(todo), "labeled": labeled}


def recluster_library(session: Session, library_id: UUID, model: str) -> dict[str, int]:
    """Group the faces that still have no person into clusters (what the "Who is this?" page shows).

    Group IDs are STABLE: a regroup moves faces between existing unnamed groups and reuses the ID of the old
    group each new group overlaps most, so a page that is open while a regroup runs still points at real groups.
    Only unnamed groups are touched; a group a person has named is never rebuilt."""
    rows = session.execute(text("""
        SELECT f.id, f.embedding, COALESCE(f.quality, 0), f.cluster_id FROM face f
         WHERE f.library_id = :l AND f.model_version = :m AND f.person_id IS NULL"""),
        {"l": library_id, "m": model}).all()
    unnamed = {r[0] for r in session.execute(
        text("SELECT id FROM face_cluster WHERE library_id = :l AND person_id IS NULL"), {"l": library_id})}
    groups = cluster_faces([r[1] for r in rows], order=[float(r[2]) for r in rows])

    # Pair each new group with the old unnamed group it shares the most faces with (each used at most once).
    overlaps = []
    for gi, g in enumerate(groups):
        counts: dict[UUID, int] = {}
        for i in g:
            old = rows[i][3]
            if old in unnamed:
                counts[old] = counts.get(old, 0) + 1
        overlaps += [(n, gi, old) for old, n in counts.items()]
    group_id: dict[int, UUID] = {}
    used: set[UUID] = set()
    for n, gi, old in sorted(overlaps, key=lambda t: (-t[0], str(t[2]))):
        if gi not in group_id and old not in used:
            group_id[gi] = old
            used.add(old)

    for gi, g in enumerate(groups):
        if gi not in group_id:
            group_id[gi] = session.execute(
                text("INSERT INTO face_cluster (library_id, model_version) VALUES (:l, :m) RETURNING id"),
                {"l": library_id, "m": model}).scalar_one()
        session.execute(text("UPDATE face SET cluster_id = :c WHERE id = ANY(:ids)"),
                        {"c": group_id[gi], "ids": [rows[i][0] for i in g]})
    # Faces that now have a person (automatic labels) leave their old unnamed group; emptied groups disappear.
    session.execute(text("UPDATE face SET cluster_id = NULL WHERE library_id = :l AND person_id IS NOT NULL "
                         "AND cluster_id IN (SELECT id FROM face_cluster WHERE library_id = :l AND person_id IS NULL)"),
                    {"l": library_id})
    session.execute(text("""DELETE FROM face_cluster c WHERE c.library_id = :l AND c.person_id IS NULL
                               AND NOT EXISTS (SELECT 1 FROM face f WHERE f.cluster_id = c.id)"""), {"l": library_id})
    return {"faces": len(rows), "clusters": len(groups)}
