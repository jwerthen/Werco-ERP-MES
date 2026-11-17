"""Request / response contracts for comments, @mentions and record watchers.

docs/NOTIFICATIONS_PLAN.md section 3.6; the service is ``app/services/comment_service.py``.

The two normalizers below are the ONE implementation of the body and mention-list rules: the
request schemas run them as field validators (a 422 with the field's location), and the
service runs them again for callers that never pass through a schema (Hank's ``add_note``).

Mentions are STRUCTURED: the client sends user ids, chosen from ``GET /users/mention-search``.
The server never parses names out of the comment text, and it re-checks every id itself.
"""

from datetime import datetime
from typing import List, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from app.schemas.base import UTCModel

COMMENT_BODY_MAX_LENGTH = 5000
MAX_MENTIONS_PER_COMMENT = 20
ENTITY_TYPE_MAX_LENGTH = 40


def normalize_comment_body(value: str) -> str:
    """The stored body: stripped, 1..5000 characters, never whitespace-only.

    Raises ``ValueError`` (a 422 through either caller). A NUL character is refused too:
    PostgreSQL ``text`` cannot store one, so letting it through would turn a typing accident
    into a 500. Nothing else is rewritten -- no markup is stripped or escaped here, because the
    body is stored verbatim and escaped at the sink (CLAUDE.md, input sanitization removed).
    """
    if not isinstance(value, str):
        raise ValueError("A comment must be text.")
    text = value.strip()
    if not text:
        raise ValueError("A comment can't be empty.")
    if len(text) > COMMENT_BODY_MAX_LENGTH:
        raise ValueError(f"A comment can be at most {COMMENT_BODY_MAX_LENGTH} characters.")
    if "\x00" in text:
        raise ValueError("A comment can't contain a NUL character.")
    return text


def normalize_mention_ids(values: Optional[Sequence[int]]) -> List[int]:
    """The mention list: at most 20 positive, distinct user ids, in the order given.

    Raises ``ValueError``. A ``bool`` is refused even though Python treats it as an int, so a
    ``true`` can never be read as user 1.
    """
    if values is None:
        return []
    ids = list(values)
    if len(ids) > MAX_MENTIONS_PER_COMMENT:
        raise ValueError(f"A comment can mention at most {MAX_MENTIONS_PER_COMMENT} people.")
    for user_id in ids:
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("Mentions must be positive user ids.")
    if len(set(ids)) != len(ids):
        raise ValueError("Each person can be mentioned only once.")
    return ids


class _CommentContent(BaseModel):
    @field_validator("body", check_fields=False)
    @classmethod
    def _body(cls, value: str) -> str:
        return normalize_comment_body(value)

    @field_validator("mentioned_user_ids", check_fields=False)
    @classmethod
    def _mentions(cls, value: List[int]) -> List[int]:
        return normalize_mention_ids(value)


class CommentCreate(_CommentContent):
    """``POST /comments``. ``entity_type`` is checked against the enabled record types by the
    service (v1: ``work_order`` only; anything else is a 422)."""

    model_config = ConfigDict(extra="forbid")

    entity_type: str = Field(..., min_length=1, max_length=ENTITY_TYPE_MAX_LENGTH)
    entity_id: StrictInt = Field(..., gt=0)
    body: str
    mentioned_user_ids: List[StrictInt] = Field(default_factory=list)


class CommentUpdate(_CommentContent):
    """``PATCH /comments/{comment_id}``: the FULL new state, both fields required. The mention
    list replaces the comment's mentions (only newly added people are notified)."""

    model_config = ConfigDict(extra="forbid")

    body: str
    mentioned_user_ids: List[StrictInt]


class WatchRequest(BaseModel):
    """``POST /watchers``."""

    model_config = ConfigDict(extra="forbid")

    entity_type: str = Field(..., min_length=1, max_length=ENTITY_TYPE_MAX_LENGTH)
    entity_id: StrictInt = Field(..., gt=0)


class CommentPerson(UTCModel):
    """A person on a comment: the author or someone mentioned. Name only -- never an email,
    role or phone."""

    id: int
    display_name: str


class CommentResponse(UTCModel):
    id: int
    entity_type: str
    entity_id: int
    author: CommentPerson
    body: str
    created_at: datetime
    edited_at: Optional[datetime] = None
    mentions: List[CommentPerson]
    # Whether THE CALLER may edit (the author only) / delete (the author, an Admin or a Manager).
    can_edit: bool
    can_delete: bool


class WatchStateResponse(UTCModel):
    """Whether the caller watches the record, and how many active people do."""

    watching: bool
    watcher_count: int


class MentionableUser(UTCModel):
    """``GET /users/mention-search`` row: ``{id, display_name}`` and nothing else."""

    id: int
    display_name: str
