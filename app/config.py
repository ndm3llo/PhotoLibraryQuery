"""Environment-driven settings. Read at call time so tests can override them."""
import os

from dotenv import load_dotenv

load_dotenv()


def supabase_url() -> str:
    url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    if not url:
        raise RuntimeError("SUPABASE_URL is not set")
    return url


def app_env() -> str:
    """'dev' enables dangerous admin endpoints. Anything else is treated as production."""
    return (os.getenv("APP_ENV") or "prod").lower()


def app_base_url() -> str:
    return (os.getenv("APP_BASE_URL") or "http://localhost:8000").rstrip("/")


INVITE_TTL_DAYS = 7


# Face matching distances: cosine distance between L2-normalised InsightFace buffalo_l embeddings.
# PROVISIONAL. Chosen from the 2026-11-21 evaluation on the labeled sample set (69-77 known faces, 15 strangers):
# the same person's nearest face was typically 0.30-0.42 away (90th percentile 0.42), while no stranger was ever
# closer than 0.79. Re-run scripts/eval_faces.py and update these (with the new numbers) when the set grows.
FACE_AUTO_MAX = 0.45      # at or below: may be applied automatically (still never counts as confirmed)
FACE_SUGGEST_MAX = 0.60   # up to here: offered as a suggestion for a person to confirm
FACE_AUTO_MIN_QUALITY = 0.5  # small or unsure faces are only ever suggestions (quality = det_score * min(1, px/112))

# "Who is this?" only asks about people who show up repeatedly: an unnamed group is offered for naming once its
# faces appear in at least this many DIFFERENT photos. Rarer faces stay quietly unnamed. Nobody is ever required
# to name everyone; a group that keeps appearing crosses the line by itself.
FACE_GROUP_MIN_PHOTOS = 3
