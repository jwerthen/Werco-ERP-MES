"""Request/response contracts for the Werco Copilot chat endpoint.

Conversation state is CLIENT-held: every request carries the full message
history and the server is stateless between turns.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class CopilotMessage(BaseModel):
    """One turn of the client-held conversation history."""

    role: Literal["user", "assistant"] = Field(..., description="Who produced this message.")
    content: str = Field(
        ...,
        min_length=1,
        max_length=8000,
        description="Plain-text message content. Long content is truncated server-side.",
    )


class CopilotChatRequest(BaseModel):
    """Stateless chat request — the client sends the full history every turn."""

    messages: List[CopilotMessage] = Field(
        ...,
        min_length=1,
        max_length=40,
        description="Conversation history, oldest first. The last message must be from the user.",
    )
    context_hint: Optional[str] = Field(
        None,
        max_length=500,
        description="Optional UI context (e.g. the page or entity the user is currently viewing).",
    )


class CopilotReference(BaseModel):
    """A deep link to an ERP entity mentioned in the answer."""

    type: str = Field(..., description="Entity type, e.g. work_order, part, customer, quote.")
    id: int = Field(..., description="Entity id within the active company.")
    label: str = Field(..., description="Human-readable identifier, e.g. the work-order number.")
    url: str = Field(..., description="Frontend route for the entity, e.g. /work-orders/123.")


class CopilotToolTraceEntry(BaseModel):
    """One read-only tool call the copilot made while answering."""

    tool: str = Field(..., description="Tool name from the copilot tool registry.")
    summary: str = Field(..., description="Human-readable one-liner, e.g. 'looked up WO-2024-0512'.")


class CopilotChatResponse(BaseModel):
    """Non-streaming (?stream=false) response; also the payload of the final SSE frame."""

    answer: str = Field(..., description="The copilot's plain-text answer.")
    references: List[CopilotReference] = Field(
        default_factory=list, description="Deep links to entities used in the answer."
    )
    tool_trace: List[CopilotToolTraceEntry] = Field(
        default_factory=list, description="Read-only tool calls made while answering, in order."
    )
    interaction_id: Optional[int] = Field(
        None, description="AIInteractionEvent id recorded for this turn (learning loop)."
    )
    rounds: int = Field(0, description="Number of tool-use rounds the model ran for this turn.")
    truncated: bool = Field(
        False, description="True when the tool-round cap was hit and the answer was forced from gathered data."
    )
