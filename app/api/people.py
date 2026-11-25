"""People: name the faces in the library, keep nicknames, merge duplicates.

Reading is open to every member. Everything that changes a name or a label needs an owner or editor
(checked here for clear errors, and again by Row Level Security in the database).

Labels a person applies are ground truth:
  manual     "this face is X" (someone said so)
  confirmed  someone accepted a suggestion
Labels made by the matcher are 'auto' and are recomputed by the `recluster_people` job (app/services/people.py).
Nothing here ever pushes a suggestion at anyone: they are computed when the groups page is opened.
"""
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.auth.deps import current_user, get_rls_session, require_role, resolve_library
from app.auth.jwt import AuthUser
from app.config import FACE_GROUP_MIN_PHOTOS
from app.db.audit import audit
from app.db.rls import service_session
from app.services import faces as face_engine
from app.services import jobs, people
from app.services.storage import get_storage

router = APIRouter()
EDIT = ("owner", "editor")
RECLUSTER_DELAY_SECONDS = 5  # lets a burst of edits collapse into one run


def _trimmed(v: str | None) -> str | None:
    """Names are trimmed, and a name of only spaces is refused (it would otherwise be stored as empty)."""
    if v is None:
        return None
    v = v.strip()
    if not v:
        raise ValueError("cannot be blank")
    return v


class _Trimmed(BaseModel):
    @field_validator("display_name", "qualifier", "new_name", "alias", check_fields=False)
    @classmethod
    def _trim(cls, v):
        return _trimmed(v)


class PersonCreate(_Trimmed):
    library_id: UUID | None = None
    display_name: str = Field(min_length=1, max_length=100)
    qualifier: str | None = Field(default=None, min_length=1, max_length=60)


class PersonPatch(_Trimmed):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    qualifier: str | None = Field(default=None, min_length=1, max_length=60)
    birth_date: date | None = None
    death_date: date | None = None


class AliasCreate(_Trimmed):
    alias: str = Field(min_length=1, max_length=100)


class GroupName(_Trimmed):
    """Name a group: either an existing person (merging it in) or a new one."""
    person_id: UUID | None = None
    new_name: str | None = Field(default=None, min_length=1, max_length=100)
    qualifier: str | None = Field(default=None, min_length=1, max_length=60)

    @model_validator(mode="after")
    def _one_of(self):
        if (self.person_id is None) == (self.new_name is None):
            raise ValueError("give exactly one of person_id or new_name")
        return self


class FaceLabel(BaseModel):
    person_id: UUID
    via: str = Field(default="manual", pattern="^(manual|suggestion)$")


class FaceReject(BaseModel):
    person_id: UUID


class Merge(BaseModel):
    into_id: UUID


class FaceIds(BaseModel):
    face_ids: list[UUID] = Field(min_length=1, max_length=200)


class FaceMove(FaceIds):
    to_person_id: UUID


# ----------------------------------------------------------------------------------- helpers

def _queue_recluster(library_id: UUID, delay: int = RECLUSTER_DELAY_SECONDS) -> None:
    with service_session() as s:
        jobs.enqueue(s, "recluster_people", {"library_id": str(library_id)},
                     dedupe_key=f"recluster:{library_id}", delay_seconds=delay)


def _person(session: Session, person_id: UUID):
    row = session.execute(text("SELECT id, library_id, display_name, profile_id FROM person WHERE id = :p"),
                          {"p": person_id}).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return row


def _editable_person(session: Session, user: AuthUser, person_id: UUID):
    row = _person(session, person_id)
    require_role(session, user.id, row.library_id, EDIT)
    return row


def _face(session: Session, face_id: UUID):
    row = session.execute(text("SELECT id, library_id, person_id FROM face WHERE id = :f"), {"f": face_id}).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Face not found")
    return row


def _same_name() -> HTTPException:
    return HTTPException(status_code=409, detail="Someone with that name already exists in this group. "
                         "Add a short description to tell them apart, for example \"cousin\".")


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


# --------------------------------------------------------------------------------- people

@router.get("")
def list_people(library_id: UUID | None = Query(None), user: AuthUser = Depends(current_user),
                session: Session = Depends(get_rls_session)):
    lib = resolve_library(session, user.id, library_id)
    rows = session.execute(text("""
        SELECT p.id, p.display_name, p.qualifier, p.birth_date, p.death_date, p.recognition_opt_out,
               (SELECT count(*) FROM face f WHERE f.person_id = p.id) AS faces,
               COALESCE((SELECT json_agg(json_build_object('id', a.id, 'alias', a.alias) ORDER BY lower(a.alias))
                           FROM person_alias a WHERE a.person_id = p.id), '[]'::json) AS alias_items
          FROM person p WHERE p.library_id = :l ORDER BY lower(p.display_name)"""), {"l": lib}).all()
    return [{"id": r.id, "display_name": r.display_name, "qualifier": r.qualifier, "birth_date": r.birth_date, "death_date": r.death_date,
             "recognition_opt_out": r.recognition_opt_out, "faces": r.faces,
             "aliases": [a["alias"] for a in r.alias_items], "alias_items": r.alias_items} for r in rows]


@router.get("/lookup")
def lookup_people(q: str = Query(..., min_length=1, max_length=100), library_id: UUID | None = Query(None),
                  limit: int = Query(8, ge=1, le=20), user: AuthUser = Depends(current_user),
                  session: Session = Depends(get_rls_session)):
    """Type-ahead for names, so people pick the exact name the group already uses instead of inventing a
    spelling. Any member may use it. `claimed` says whether the person is already linked to an account."""
    lib = resolve_library(session, user.id, library_id)
    rows = session.execute(text("""
        SELECT p.id, p.display_name, p.qualifier, p.profile_id IS NOT NULL AS claimed,
               COALESCE((SELECT array_agg(a.alias) FROM person_alias a WHERE a.person_id = p.id), '{}') AS aliases
          FROM person p WHERE p.library_id = :l"""), {"l": lib}).all()
    found = people.rank_names(q, [{"id": r.id, "display_name": r.display_name, "qualifier": r.qualifier,
                                   "claimed": r.claimed, "aliases": list(r.aliases)} for r in rows], limit)
    return [{k: c[k] for k in ("id", "display_name", "qualifier", "claimed", "via_alias")} for c in found]


@router.post("", status_code=201)
def create_person(body: PersonCreate, request: Request, user: AuthUser = Depends(current_user),
                  session: Session = Depends(get_rls_session)):
    lib = resolve_library(session, user.id, body.library_id, EDIT)
    qualifier = body.qualifier.strip() if body.qualifier else None
    try:
        pid = session.execute(text("INSERT INTO person (library_id, display_name, qualifier, created_by) "
                                   "VALUES (:l, :n, :q, :u) RETURNING id"),
                              {"l": lib, "n": body.display_name.strip(), "q": qualifier, "u": user.id}).scalar_one()
        session.commit()
    except IntegrityError:
        session.rollback()
        raise _same_name()
    audit(user.id, "person.create", library_id=lib, target=str(pid), ip=_ip(request))
    return {"id": pid, "library_id": lib, "display_name": body.display_name.strip(), "qualifier": qualifier}


@router.patch("/{person_id}")
def update_person(person_id: UUID, body: PersonPatch, user: AuthUser = Depends(current_user),
                  session: Session = Depends(get_rls_session)):
    _editable_person(session, user, person_id)
    fields = body.model_dump(exclude_unset=True)
    if "display_name" in fields:
        if fields["display_name"] is None:
            raise HTTPException(status_code=422, detail="display_name cannot be empty")
        fields["display_name"] = fields["display_name"].strip()
    if "qualifier" in fields and fields["qualifier"] is not None:
        fields["qualifier"] = fields["qualifier"].strip()
    if not fields:
        raise HTTPException(status_code=422, detail="nothing to change")
    sets = ", ".join(f"{k} = :{k}" for k in fields)  # keys come from the model above, never from the client
    try:
        session.execute(text(f"UPDATE person SET {sets} WHERE id = :p"), {**fields, "p": person_id})
        session.commit()
    except IntegrityError:
        session.rollback()
        raise _same_name()
    return {"id": person_id, **fields}


@router.delete("/{person_id}", status_code=204)
def delete_person(person_id: UUID, request: Request, user: AuthUser = Depends(current_user),
                  session: Session = Depends(get_rls_session)):
    """Deletes the person and their nicknames. Their faces stay in the photos, unlabeled."""
    row = _editable_person(session, user, person_id)
    session.execute(text("UPDATE face SET person_id = NULL, person_source = NULL, person_confidence = NULL "
                         "WHERE person_id = :p"), {"p": person_id})
    session.execute(text("DELETE FROM person WHERE id = :p"), {"p": person_id})
    session.commit()
    audit(user.id, "person.delete", library_id=row.library_id, target=str(person_id), ip=_ip(request))
    _queue_recluster(row.library_id)


@router.post("/{person_id}/merge")
def merge_person(person_id: UUID, body: Merge, request: Request, user: AuthUser = Depends(current_user),
                 session: Session = Depends(get_rls_session)):
    """Fold a duplicate into another person: faces move, nicknames (and the old name) become nicknames."""
    src = _editable_person(session, user, person_id)
    dst = _person(session, body.into_id)
    if src.id == dst.id:
        raise HTTPException(status_code=422, detail="Cannot merge a person into themselves")
    if src.library_id != dst.library_id:
        raise HTTPException(status_code=404, detail="Person not found")
    if src.profile_id is not None:
        raise HTTPException(status_code=409, detail="This person is linked to an account; merge the other one into them")
    if session.execute(text("SELECT 1 FROM person_reference WHERE person_id = :p LIMIT 1"), {"p": src.id}).first():
        raise HTTPException(status_code=409, detail="This person has enrolled selfies; merge the other one into them")

    session.execute(text("UPDATE face SET person_id = :d, person_source = 'manual', person_confidence = NULL "
                         "WHERE person_id = :s"), {"d": dst.id, "s": src.id})
    session.execute(text("UPDATE face_cluster SET person_id = :d WHERE person_id = :s"), {"d": dst.id, "s": src.id})
    session.execute(text("""
        INSERT INTO person_alias (library_id, person_id, alias, created_by)
        SELECT :l, :d, a, :u FROM (SELECT alias AS a FROM person_alias WHERE person_id = :s
                                   UNION SELECT :sname) x
         WHERE lower(btrim(a)) <> lower(btrim(:dname))
        ON CONFLICT DO NOTHING"""),
        {"l": src.library_id, "d": dst.id, "s": src.id, "u": user.id, "sname": src.display_name,
         "dname": dst.display_name})
    session.execute(text("DELETE FROM person WHERE id = :s"), {"s": src.id})
    session.commit()
    audit(user.id, "person.merge", library_id=src.library_id, target=str(src.id),
          meta={"into": str(dst.id)}, ip=_ip(request))
    _queue_recluster(src.library_id)
    return {"id": dst.id}


# ------------------------------------------------------- review and correct a person's faces

@router.get("/{person_id}/faces")
def person_faces(person_id: UUID, limit: int = Query(60, ge=1, le=200), offset: int = Query(0, ge=0),
                 best: bool = Query(False), user: AuthUser = Depends(current_user), session: Session = Depends(get_rls_session)):
    """The faces labeled as this person. By default for REVIEW: automatic labels first, least confident first (the
    likeliest mistakes), then labels a person made. With best=true for SHOWING who someone is: labels a person made
    first, then the clearest faces."""
    _person(session, person_id)
    total = session.execute(text("SELECT count(*) FROM face WHERE person_id = :p"), {"p": person_id}).scalar()
    rows = session.execute(text("""
        SELECT f.id, f.photo_id, f.bbox, f.person_source, f.person_confidence, p.width, p.height, p.thumb_key
          FROM face f JOIN photo p ON p.id = f.photo_id WHERE f.person_id = :p
         ORDER BY """ + ("(f.person_source = 'auto') ASC, f.quality DESC NULLS LAST, f.id" if best else
                         "(f.person_source = 'auto') DESC, f.person_confidence ASC NULLS LAST, f.id") + """
         LIMIT :n OFFSET :o"""), {"p": person_id, "n": limit, "o": offset}).all()
    storage = get_storage()
    return {"total": total, "faces": [
        {"id": r.id, "photo_id": r.photo_id, "bbox": r.bbox, "source": r.person_source,
         "confidence": r.person_confidence, "photo_width": r.width, "photo_height": r.height,
         "thumbnail_url": storage.presign_get(r.thumb_key) if r.thumb_key else None} for r in rows]}


@router.post("/{person_id}/faces/remove")
def remove_faces(person_id: UUID, body: FaceIds, user: AuthUser = Depends(current_user),
                 session: Session = Depends(get_rls_session)):
    """'These faces are not this person.' They become unlabeled, and this person is remembered as rejected for
    each, so it is never suggested or applied automatically again. Faces that are not theirs are ignored."""
    person = _editable_person(session, user, person_id)
    found = [r[0] for r in session.execute(text("SELECT id FROM face WHERE id = ANY(:ids) AND person_id = :p"),
                                           {"ids": body.face_ids, "p": person_id})]
    if found:
        session.execute(text("""INSERT INTO face_rejection (face_id, person_id, library_id, rejected_by)
                                SELECT f.id, :p, f.library_id, :u FROM face f WHERE f.id = ANY(:ids)
                                ON CONFLICT DO NOTHING"""), {"p": person_id, "u": user.id, "ids": found})
        session.execute(text("UPDATE face SET person_id = NULL, person_source = NULL, person_confidence = NULL "
                             "WHERE id = ANY(:ids)"), {"ids": found})
    session.commit()
    if found:
        _queue_recluster(person.library_id)
    return {"removed": len(found)}


@router.post("/{person_id}/faces/move")
def move_faces(person_id: UUID, body: FaceMove, user: AuthUser = Depends(current_user),
               session: Session = Depends(get_rls_session)):
    """'These faces are someone else': move them to another person (a decision by a person, so ground truth).
    The old person is remembered as rejected for each. Faces that are not theirs are ignored."""
    src = _editable_person(session, user, person_id)
    dst = _person(session, body.to_person_id)
    if dst.library_id != src.library_id or dst.id == src.id:
        raise HTTPException(status_code=404 if dst.library_id != src.library_id else 422,
                            detail="Person not found" if dst.library_id != src.library_id
                            else "Choose a different person to move the faces to")
    found = [r[0] for r in session.execute(text("SELECT id FROM face WHERE id = ANY(:ids) AND person_id = :p"),
                                           {"ids": body.face_ids, "p": person_id})]
    if found:
        session.execute(text("""INSERT INTO face_rejection (face_id, person_id, library_id, rejected_by)
                                SELECT f.id, :p, f.library_id, :u FROM face f WHERE f.id = ANY(:ids)
                                ON CONFLICT DO NOTHING"""), {"p": person_id, "u": user.id, "ids": found})
        session.execute(text("DELETE FROM face_rejection WHERE person_id = :d AND face_id = ANY(:ids)"),
                        {"d": dst.id, "ids": found})
        session.execute(text("UPDATE face SET person_id = :d, person_source = 'manual', person_confidence = NULL "
                             "WHERE id = ANY(:ids)"), {"d": dst.id, "ids": found})
    session.commit()
    if found:
        _queue_recluster(src.library_id)
    return {"moved": len(found)}


# -------------------------------------------------------------------------------- nicknames

@router.post("/{person_id}/aliases", status_code=201)
def add_alias(person_id: UUID, body: AliasCreate, user: AuthUser = Depends(current_user),
              session: Session = Depends(get_rls_session)):
    row = _editable_person(session, user, person_id)
    alias = body.alias.strip()
    if not alias:
        raise HTTPException(status_code=422, detail="alias cannot be blank")
    try:
        aid = session.execute(text("INSERT INTO person_alias (library_id, person_id, alias, created_by) "
                                   "VALUES (:l, :p, :a, :u) RETURNING id"),
                              {"l": row.library_id, "p": person_id, "a": alias, "u": user.id}).scalar_one()
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="That nickname already exists for this person")
    return {"id": aid, "alias": alias}


@router.delete("/{person_id}/aliases/{alias_id}", status_code=204)
def remove_alias(person_id: UUID, alias_id: UUID, user: AuthUser = Depends(current_user),
                 session: Session = Depends(get_rls_session)):
    _editable_person(session, user, person_id)
    n = session.execute(text("DELETE FROM person_alias WHERE id = :a AND person_id = :p"),
                        {"a": alias_id, "p": person_id}).rowcount
    session.commit()
    if not n:
        raise HTTPException(status_code=404, detail="Nickname not found")


# ---------------------------------------------------------------- "Who is this?" groups

@router.get("/groups")
def list_groups(library_id: UUID | None = Query(None), limit: int = Query(30, ge=1, le=100),
                min_photos: int = Query(FACE_GROUP_MIN_PHOTOS, ge=1, le=1000),
                user: AuthUser = Depends(current_user), session: Session = Depends(get_rls_session)):
    """Groups of faces nobody has named yet that show up REPEATEDLY (in at least `min_photos` different photos),
    largest first, each with a few sample faces and (when there is one) a suggested person. Rarer faces are not
    listed, only counted in `hidden`: nobody has to name everyone. Suggestions exist only in this response;
    nothing is stored or pushed."""
    lib = resolve_library(session, user.id, library_id, EDIT)
    model = face_engine.MODEL_VERSION
    all_groups = session.execute(text("""
        SELECT c.id, count(*) AS size, count(DISTINCT f.photo_id) AS photos
          FROM face_cluster c JOIN face f ON f.cluster_id = c.id
         WHERE c.library_id = :l AND c.person_id IS NULL AND f.person_id IS NULL AND f.model_version = :m
         GROUP BY c.id ORDER BY photos DESC, size DESC, c.id"""), {"l": lib, "m": model}).all()
    clusters = [c for c in all_groups if c.photos >= min_photos][:limit]
    hidden = sum(1 for c in all_groups if c.photos < min_photos)
    if not clusters:
        return {"groups": [], "hidden": hidden, "min_photos": min_photos}
    ids = [c.id for c in clusters]
    samples = session.execute(text("""
        SELECT * FROM (
          SELECT f.id, f.cluster_id, f.photo_id, f.bbox, f.quality, f.embedding, p.width, p.height, p.thumb_key,
                 row_number() OVER (PARTITION BY f.cluster_id ORDER BY f.quality DESC NULLS LAST, f.id) AS rn
            FROM face f JOIN photo p ON p.id = f.photo_id
           WHERE f.cluster_id = ANY(:ids) AND f.person_id IS NULL) t
         WHERE rn <= 6 ORDER BY cluster_id, rn"""), {"ids": ids}).all()

    gallery = people._gallery(session, lib, model)
    opted_out = frozenset(r[0] for r in session.execute(
        text("SELECT id FROM person WHERE library_id = :l AND recognition_opt_out"), {"l": lib}))
    names = {r[0]: r[1] for r in session.execute(text("SELECT id, display_name FROM person WHERE library_id = :l"),
                                                 {"l": lib})}
    storage = get_storage()
    by_cluster: dict[UUID, list] = {}
    for r in samples:
        by_cluster.setdefault(r.cluster_id, []).append(r)

    out = []
    for c in clusters:
        faces = by_cluster.get(c.id, [])
        suggestion = None
        if faces and gallery:
            best = faces[0]
            rejected = frozenset(r[0] for r in session.execute(
                text("SELECT person_id FROM face_rejection WHERE face_id = :f"), {"f": best.id}))
            m = people.match_face(best.embedding, float(best.quality or 0), gallery, excluded=opted_out | rejected)
            if m:
                suggestion = {"person_id": m.person_id, "display_name": names.get(m.person_id),
                              "distance": m.distance}
        out.append({
            "id": c.id, "size": c.size, "photos": c.photos, "suggestion": suggestion,
            "faces": [{"id": f.id, "photo_id": f.photo_id, "bbox": f.bbox, "photo_width": f.width,
                       "photo_height": f.height,
                       "thumbnail_url": storage.presign_get(f.thumb_key) if f.thumb_key else None} for f in faces]})
    return {"groups": out, "hidden": hidden, "min_photos": min_photos}


@router.post("/groups/{cluster_id}/name")
def name_group(cluster_id: UUID, body: GroupName, request: Request, user: AuthUser = Depends(current_user),
               session: Session = Depends(get_rls_session)):
    """Say who a whole group is: an existing person (the group is merged in) or a new one."""
    cluster = session.execute(text("SELECT id, library_id FROM face_cluster WHERE id = :c"), {"c": cluster_id}).first()
    if cluster is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_role(session, user.id, cluster.library_id, EDIT)
    if body.person_id:
        person = _person(session, body.person_id)
        if person.library_id != cluster.library_id:
            raise HTTPException(status_code=404, detail="Person not found")
        pid = person.id
    else:
        try:
            pid = session.execute(text("INSERT INTO person (library_id, display_name, qualifier, created_by) "
                                       "VALUES (:l, :n, :q, :u) RETURNING id"),
                                  {"l": cluster.library_id, "n": body.new_name.strip(),
                                   "q": body.qualifier.strip() if body.qualifier else None,
                                   "u": user.id}).scalar_one()
        except IntegrityError:
            session.rollback()
            raise _same_name()
    n = session.execute(text("UPDATE face SET person_id = :p, person_source = 'manual', person_confidence = NULL "
                             "WHERE cluster_id = :c AND person_id IS NULL"), {"p": pid, "c": cluster_id}).rowcount
    session.execute(text("UPDATE face_cluster SET person_id = :p WHERE id = :c"), {"p": pid, "c": cluster_id})
    session.commit()
    audit(user.id, "group.name", library_id=cluster.library_id, target=str(pid), meta={"faces": n}, ip=_ip(request))
    _queue_recluster(cluster.library_id)
    return {"person_id": pid, "faces_named": n}


@router.post("/groups/{cluster_id}/reject", status_code=204)
def reject_group(cluster_id: UUID, body: FaceReject, user: AuthUser = Depends(current_user),
                 session: Session = Depends(get_rls_session)):
    """'None of these faces is X': remembered for every unnamed face in the group."""
    cluster = session.execute(text("SELECT id, library_id FROM face_cluster WHERE id = :c"), {"c": cluster_id}).first()
    if cluster is None:
        raise HTTPException(status_code=404, detail="Group not found")
    require_role(session, user.id, cluster.library_id, EDIT)
    person = _person(session, body.person_id)
    if person.library_id != cluster.library_id:
        raise HTTPException(status_code=404, detail="Person not found")
    session.execute(text("""INSERT INTO face_rejection (face_id, person_id, library_id, rejected_by)
                            SELECT f.id, :p, f.library_id, :u FROM face f
                             WHERE f.cluster_id = :c AND f.person_id IS NULL
                            ON CONFLICT DO NOTHING"""), {"p": person.id, "u": user.id, "c": cluster_id})
    session.commit()


# ----------------------------------------------------------------------- single faces

@router.post("/faces/{face_id}/label")
def label_face(face_id: UUID, body: FaceLabel, user: AuthUser = Depends(current_user),
               session: Session = Depends(get_rls_session)):
    """'This face is X' (manual), or accepting a suggestion (confirmed). Both count as ground truth."""
    face = _face(session, face_id)
    require_role(session, user.id, face.library_id, EDIT)
    person = _person(session, body.person_id)
    if person.library_id != face.library_id:
        raise HTTPException(status_code=404, detail="Person not found")
    session.execute(text("DELETE FROM face_rejection WHERE face_id = :f AND person_id = :p"),
                    {"f": face_id, "p": person.id})
    session.execute(text("UPDATE face SET person_id = :p, person_source = :s, person_confidence = NULL WHERE id = :f"),
                    {"p": person.id, "s": "confirmed" if body.via == "suggestion" else "manual", "f": face_id})
    session.commit()
    _queue_recluster(face.library_id)
    return {"face_id": face_id, "person_id": person.id}


@router.post("/faces/{face_id}/reject", status_code=204)
def reject_face(face_id: UUID, body: FaceReject, user: AuthUser = Depends(current_user),
                session: Session = Depends(get_rls_session)):
    """'This face is NOT X'. Remembered, so X is never suggested or auto-applied for this face again."""
    face = _face(session, face_id)
    require_role(session, user.id, face.library_id, EDIT)
    person = _person(session, body.person_id)
    if person.library_id != face.library_id:
        raise HTTPException(status_code=404, detail="Person not found")
    session.execute(text("INSERT INTO face_rejection (face_id, person_id, library_id, rejected_by) "
                         "VALUES (:f, :p, :l, :u) ON CONFLICT DO NOTHING"),
                    {"f": face_id, "p": person.id, "l": face.library_id, "u": user.id})
    if face.person_id == person.id:
        session.execute(text("UPDATE face SET person_id = NULL, person_source = NULL, person_confidence = NULL "
                             "WHERE id = :f"), {"f": face_id})
    session.commit()
    _queue_recluster(face.library_id)
