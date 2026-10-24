"""
Pydantic contracts for every LLM extraction task.

Rule of thumb used throughout: the model extracts WHAT the user said (names, phrases like
"tomorrow at 5"), and deterministic code works out the rest (ids, datetimes, permissions).
That keeps each model output small, which is where free models are most reliable.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

AGENTS = (
    "scheduler", "memory", "planner", "communication", "device",
    "github", "places", "briefing", "chat",
)
AgentName = Literal[
    "scheduler", "memory", "planner", "communication", "device",
    "github", "places", "briefing", "chat",
]


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @field_validator("*", mode="before")
    @classmethod
    def _normalise(cls, v, info):
        # Enum-like fields: accept "Create", " create ", "CREATE".
        if info.field_name in {"agent", "action", "operation", "relation", "type", "node_type", "mode", "tone"} \
                and isinstance(v, str):
            return v.strip().lower().replace(" ", "_").replace("-", "_")
        # Models often send "" for "not mentioned".
        if isinstance(v, str) and v.strip().lower() in {"", "null", "none", "n/a"}:
            return None
        return v


# ---------------------------------------------------------------- routing
class RouteOut(_Lenient):
    agent: AgentName
    confidence: float = Field(0.7, ge=0, le=1)
    clarify: Optional[str] = None  # one short question if the request is genuinely ambiguous


class SplitOut(_Lenient):
    steps: list[str] = Field(min_length=1, max_length=6)


# ---------------------------------------------------------------- scheduler
class CalendarOut(_Lenient):
    action: Literal["list", "check", "create", "move", "cancel", "find_slot"]
    title: Optional[str] = None
    when: Optional[str] = None            # natural phrase, e.g. "tomorrow 5pm", "friday"
    duration_minutes: Optional[int] = Field(None, ge=5, le=24 * 60)
    attendees: list[str] = Field(default_factory=list)
    location: Optional[str] = None
    target: Optional[str] = None          # which existing event (title words) for move/cancel
    new_when: Optional[str] = None        # for move


# ---------------------------------------------------------------- memory
class MemoryQueryOut(_Lenient):
    operation: Literal["about", "related", "list", "connected", "search"]
    entity: Optional[str] = None          # e.g. "Arun"
    relation: Optional[str] = None        # e.g. "introduced_by", "works_on", "owes"
    node_type: Optional[str] = None       # for list: person/meeting/task/commitment/place/project
    target: Optional[str] = None          # for connected
    text: Optional[str] = None            # for search


class EntityOut(_Lenient):
    name: str
    type: Literal["person", "meeting", "task", "commitment", "place", "project", "preference"]


class RelationOut(_Lenient):
    source: str = Field(alias="from")
    relation: Literal[
        "relates_to", "follows_up", "blocks", "introduced_by", "attended", "owes",
        "works_on", "meets_at", "prefers", "works_with",
    ]
    target: str = Field(alias="to")
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class AttributeOut(_Lenient):
    entity: str
    key: str
    value: str


class MemoryFactOut(_Lenient):
    entities: list[EntityOut] = Field(min_length=1)
    relations: list[RelationOut] = Field(default_factory=list)
    attributes: list[AttributeOut] = Field(default_factory=list)


# ---------------------------------------------------------------- communication
class MessageOut(_Lenient):
    action: Literal["draft_email", "read_inbox", "search_email", "reply_email",
                    "draft_slack", "read_slack"]
    to: list[str] = Field(default_factory=list)       # names or emails / slack users
    channel: Optional[str] = None                     # slack channel
    subject: Optional[str] = None
    intent: Optional[str] = None                      # what the message should say
    query: Optional[str] = None                       # search terms / which mail to reply to
    tone: Optional[str] = None


class DraftOut(_Lenient):
    subject: Optional[str] = None
    body: str = Field(min_length=1)


# ---------------------------------------------------------------- device
class DeviceOut(_Lenient):
    action: Literal["set_alarm", "set_timer", "open_app", "call", "send_sms", "find_contact",
                    "get_location", "read_notifications", "toggle", "battery", "set_reminder"]
    time: Optional[str] = None
    minutes: Optional[int] = Field(None, ge=1, le=24 * 60)
    label: Optional[str] = None
    app: Optional[str] = None
    contact: Optional[str] = None
    message: Optional[str] = None
    setting: Optional[Literal["wifi", "bluetooth", "flashlight", "dnd", "silent"]] = None
    state: Optional[bool] = None


# ---------------------------------------------------------------- github
class GithubOut(_Lenient):
    action: Literal["notifications", "list_prs", "list_issues", "create_issue", "ci_status",
                    "repo_summary", "comment_issue"]
    repo: Optional[str] = None
    title: Optional[str] = None
    body: Optional[str] = None
    number: Optional[int] = None


# ---------------------------------------------------------------- places
class PlacesOut(_Lenient):
    action: Literal["find_place", "travel_time", "meeting_point", "when_to_leave"]
    place: Optional[str] = None
    origin: Optional[str] = None
    destination: Optional[str] = None
    people: list[str] = Field(default_factory=list)
    mode: Optional[Literal["driving", "walking", "cycling", "transit"]] = None
    event: Optional[str] = None
