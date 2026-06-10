"""Werco Copilot chat endpoint — read-only ask-anything over tenant ERP data.

POST /api/v1/copilot/chat

- Any authenticated user (individual tools mirror their source endpoints'
  access rules; see ``app/services/copilot_service.py``).
- Conversation state is client-held: the request carries the full history and
  the server is stateless between turns.
- Default response is a Server-Sent-Events stream (``text/event-stream``) of
  JSON frames: ``tool_use`` activity hints, ``delta`` answer chunks, then one
  ``final`` frame matching :class:`CopilotChatResponse`. Pass ``?stream=false``
  for a plain JSON response.
- Per-user rate limit (default 20 requests/minute, ``COPILOT_RATE_LIMIT_PER_MINUTE``)
  on top of the app-wide slowapi per-IP limits configured in ``app/main.py``.
"""

import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Generator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, get_current_user
from app.db.database import get_db
from app.models.user import User
from app.schemas.copilot import CopilotChatRequest, CopilotChatResponse
from app.services.copilot_service import CopilotService
from app.services.llm_client import LLMNotConfiguredError

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Per-user rate limit (in-process sliding window).
# The global slowapi middleware in app/main.py is keyed by client IP; copilot
# turns are expensive (multi-call LLM loops), so we add a per-user budget here.
# TODO(redis): move this per-user limiter to Redis so the budget is shared across processes/replicas.
# ---------------------------------------------------------------------------
COPILOT_RATE_LIMIT_PER_MINUTE = int(os.getenv("COPILOT_RATE_LIMIT_PER_MINUTE", "20"))
_RATE_WINDOW_SECONDS = 60.0
_rate_buckets: Dict[int, Deque[float]] = defaultdict(deque)
_rate_lock = threading.Lock()


def _check_rate_limit(user_id: int) -> None:
    now = time.monotonic()
    with _rate_lock:
        bucket = _rate_buckets[user_id]
        while bucket and now - bucket[0] > _RATE_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= COPILOT_RATE_LIMIT_PER_MINUTE:
            raise HTTPException(status_code=429, detail="Copilot rate limit exceeded; try again in a minute.")
        bucket.append(now)


def _sse_frame(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


@router.post("/chat", response_model=None)
def copilot_chat(
    request: CopilotChatRequest,
    stream: bool = Query(
        default=True,
        description="Stream the answer as Server-Sent Events (default). Set false for a plain JSON response.",
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Ask the read-only Werco Copilot a question about your company's ERP data.

    The copilot answers via tool calls against existing read endpoints (work
    orders, blockers, schedule load/conflicts, inventory, customers, search).
    It cannot create, update, or delete anything.

    Streaming frames (``data: <json>``):
    - ``{"type": "tool_use", "tool": ..., "summary": ...}`` — a lookup ran
    - ``{"type": "delta", "text": ...}`` — answer text chunk
    - ``{"type": "final", ...}`` — full :class:`CopilotChatResponse` payload
    - ``{"type": "error", "message": ...}`` — terminal error frame
    """
    if request.messages[-1].role != "user":
        raise HTTPException(status_code=422, detail="The last message must be from the user.")

    # Consume a rate-limit token only AFTER request validation, so malformed
    # requests don't burn the caller's per-minute budget.
    _check_rate_limit(current_user.id)

    service = CopilotService(db, company_id=company_id, user=current_user)
    plain_messages = [{"role": m.role, "content": m.content} for m in request.messages]

    if not stream:
        try:
            final = service.run_chat(messages=plain_messages, context_hint=request.context_hint)
        except LLMNotConfiguredError:
            raise HTTPException(status_code=503, detail="AI assistant is not configured on this server.")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception as exc:
            logger.exception("Copilot chat failed")
            raise HTTPException(status_code=502, detail=f"AI service error: {type(exc).__name__}")
        db.commit()
        return CopilotChatResponse(**final)

    def event_stream() -> Generator[str, None, None]:
        try:
            for event in service.stream_chat(messages=plain_messages, context_hint=request.context_hint):
                yield _sse_frame(event)
            db.commit()
        except LLMNotConfiguredError:
            yield _sse_frame({"type": "error", "message": "AI assistant is not configured on this server."})
        except ValueError as exc:
            yield _sse_frame({"type": "error", "message": str(exc)})
        except Exception as exc:
            logger.exception("Copilot chat stream failed")
            yield _sse_frame({"type": "error", "message": f"AI service error: {type(exc).__name__}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
