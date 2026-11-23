"""Searching the library with text (plan §6.5 step 3): the query is embedded with
CLIP's text encoder and scored against every item's vector in the similarity
matrix (`photo_triage.api.similarity`). The items at or above the embedder's floor
(step 4) are the matches the map highlights (plan §6.4 "Search").

The text encoder is loaded on the first search, since the API needs no model
otherwise: 255 MB for ViT-B-32, beside the matrix's 200 MB for 100k items, within
the app's 1 GB cap (plan §6.11). With `WORKER_FAKE_CLIP`, as in `just e2e`, it's
`FakeClip`, so search runs without the weights the worker didn't use either.

Person filters and keyword matches (steps 1 and 2) come with faces and tagging.
"""

import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from photo_triage.api.deps import DbSession, get_similarity, get_text_encoder
from photo_triage.api.similarity import Similarity
from photo_triage.api.text_encoder import TextEncoder
from photo_triage.ml import ModelMissingError

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_QUERY_CHARS = 500
"""Far longer than CLIP reads: its 77 tokens are about 300 characters of English."""


class Match(BaseModel):
    id: int
    score: float
    """The cosine similarity between the query and the item, at least the floor."""


class SearchResults(BaseModel):
    query: str
    floor: float
    """The score a match needs."""
    matches: list[Match]
    """Best first."""


@router.get(
    "/search",
    responses={503: {"description": "The CLIP model isn't in MODELS_DIR"}},
)
def search(
    q: Annotated[str, Query(min_length=1, max_length=MAX_QUERY_CHARS)],
    session: DbSession,
    encoder: Annotated[TextEncoder, Depends(get_text_encoder)],
    similarity: Annotated[Similarity, Depends(get_similarity)],
) -> SearchResults:
    """The items that match `q`, by CLIP similarity, best first (plan §6.5)."""
    query = q.strip()
    if not query:
        raise HTTPException(422, "The query is empty")
    started_s = time.monotonic()
    try:
        embedder = encoder.embedder()
    except ModelMissingError as error:
        raise HTTPException(503, str(error)) from None
    vector = embedder.embed_texts([query])[0]
    found = similarity.matches(session, vector, embedder.search_floor)
    logger.debug(
        "Search %r: %d matches in %.0f ms",
        query,
        len(found),
        (time.monotonic() - started_s) * 1000,
    )
    return SearchResults(
        query=query,
        floor=embedder.search_floor,
        matches=[Match(id=item_id, score=score) for item_id, score in found],
    )
