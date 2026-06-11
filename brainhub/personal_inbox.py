"""Shared Personal Inbox core for daily review workflows.

The Personal Inbox is intentionally separate from any Obsidian vault. This
module owns the inbox data shape and read/query helpers that host adapters can
share without duplicating approval logic.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

APPROVED_STATUS = "approved"
DEFAULT_INBOX_DIR = "personal_inbox"
DEFAULT_INBOX_FILE = "items.json"
DEFAULT_VAULT_FOLDERS = ("Daily", "Inbox", "Projects", "Knowledge", "Reviews")
INBOX_STORE_KIND = "personal_inbox"
SESSION_STORE_RELATIVE_PATH = Path("state.db")
MEMORY_STORE_RELATIVE_PATHS = (
    Path("memories"),
    Path("plugins") / "memory",
)
ITEM_TYPES = (
    "task",
    "schedule",
    "idea",
    "memory",
    "project note",
    "knowledge note",
    "blog candidate",
    "report candidate",
)
DESTINATIONS = ("Daily", "Inbox", "Projects", "Knowledge")
PRIORITIES = ("", "none", "low", "medium", "high", "urgent")
APPROVAL_STATUSES = (
    "pending",
    APPROVED_STATUS,
    "edited",
    "deferred",
    "archived",
    "frozen",
    "trash",
)
EDITABLE_DRAFT_FIELDS = (
    "short_summary",
    "recommended_type",
    "recommended_destination",
    "priority",
    "due_date",
    "schedule_date",
    "next_action",
    "linked_project",
    "linked_knowledge_topic",
    "blog_candidate",
    "report_candidate",
    "obsidian_markdown",
    "vault_write_target",
)
INTERNAL_REVIEW_FIELDS = ("approved_at", "reviewed_at", "vault_written_at")
PRIORITY_FIELDS = (
    "priority",
    "due_date",
    "schedule_date",
    "next_action",
    "linked_project",
    "linked_knowledge_topic",
    "blog_candidate",
    "report_candidate",
)
_REVIEW_DATE_FIELDS = (
    "approved_at",
    "reviewed_at",
    "vault_written_at",
    "updated_at",
    "created_at",
)
DEFAULT_DAILY_PRIORITY_LIMIT = 5
DEFAULT_REFLECTION_QUESTION_LIMIT = 5
SOURCE_METADATA_FIELDS = (
    "source_channel",
    "surface",
    "platform",
    "conversation_id",
    "thread_id",
    "user_id",
    "message_id",
    "source_url",
    "captured_at",
    "timezone",
    "metadata",
)
RAW_TEXT_FIELDS = ("original_text", "text", "content", "body", "message")


def default_inbox_path() -> Path:
    """Return the default profile-aware Personal Inbox JSON path."""

    return get_brainhub_home() / DEFAULT_INBOX_DIR / DEFAULT_INBOX_FILE


def get_brainhub_home() -> Path:
    """Return the BrainHub home directory used for local state."""

    raw_home = (
        os.getenv("BRAINHUB_HOME", "").strip()
        or os.getenv("HERMES_HOME", "").strip()
    )
    if raw_home:
        return Path(raw_home).expanduser()
    return Path.home() / ".brainhub"


def configured_obsidian_vault_path() -> Path | None:
    """Return the configured Obsidian vault path, if one is available."""

    raw_env = (
        os.getenv("BRAINHUB_OBSIDIAN_VAULT_PATH", "").strip()
        or os.getenv("OBSIDIAN_VAULT_PATH", "").strip()
    )
    if raw_env:
        return Path(raw_env).expanduser()
    return None


def _resolve_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_inbox_store_path(path: Path) -> Path:
    """Reject accidental reuse of host session or memory persistence paths."""

    resolved = _resolve_path(path)
    brainhub_home = _resolve_path(get_brainhub_home())
    session_store = _resolve_path(brainhub_home / SESSION_STORE_RELATIVE_PATH)
    if resolved == session_store:
        raise ValueError("Personal Inbox store must not use host session state.db")

    for relative_path in MEMORY_STORE_RELATIVE_PATHS:
        memory_path = _resolve_path(brainhub_home / relative_path)
        if resolved == memory_path or _is_relative_to(resolved, memory_path):
            raise ValueError("Personal Inbox store must not use host memory storage paths")

    return resolved


def inbox_store_path_is_isolated(path: str | Path | None = None) -> bool:
    """Return whether ``path`` is separate from session and memory stores."""

    try:
        _validate_inbox_store_path(Path(path) if path is not None else default_inbox_path())
    except ValueError:
        return False
    return True


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _new_item_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return f"cap_{stamp}_{uuid.uuid4().hex[:8]}"


def _summarize(text: str, limit: int = 80) -> str:
    compact = " ".join(text.strip().split())
    return compact if len(compact) <= limit else f"{compact[: limit - 1]}..."


def _infer_type(text: str) -> str:
    lowered = text.lower()
    if any(token in lowered for token in ("blog", "블로그", "post", "글감")):
        return "blog candidate"
    if any(token in lowered for token in ("report", "리포트", "뉴스", "소식")):
        return "report candidate"
    if any(token in lowered for token in ("todo", "해야", "할일", "task", "next action")):
        return "task"
    if any(token in lowered for token in ("일정", "calendar", "schedule", "예약", "미팅")):
        return "schedule"
    if any(token in lowered for token in ("프로젝트", "project", "roadmap", "로드맵")):
        return "project note"
    if any(token in lowered for token in ("깨달", "배운", "학습", "knowledge", "insight")):
        return "knowledge note"
    return "memory"


def _destination_for_type(item_type: str) -> str:
    if item_type in {"task", "schedule", "memory"}:
        return "Daily"
    if item_type == "project note":
        return "Projects"
    if item_type == "knowledge note":
        return "Knowledge"
    return "Inbox"


def _slug(text: str, fallback: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z가-힣._ -]+", "", text).strip().lower()
    normalized = re.sub(r"\s+", "-", normalized)
    return (normalized or fallback).strip(".-/")[:80] or fallback


def _default_vault_target(item: "PersonalInboxItem") -> str:
    today = datetime.now().astimezone().date().isoformat()
    summary = _slug(item.short_summary or item.original_text, item.item_id)
    if item.recommended_destination == "Daily":
        return f"Daily/{today}.md"
    if item.recommended_destination == "Projects":
        project = _slug(item.linked_project or summary, item.item_id)
        return f"Projects/{project}.md"
    if item.recommended_destination == "Knowledge":
        topic = _slug(item.linked_knowledge_topic or summary, item.item_id)
        return f"Knowledge/{topic}.md"
    return f"Inbox/{today}-{summary}.md"


def build_obsidian_markdown(item: "PersonalInboxItem") -> str:
    """Build a small Markdown note for an approved Inbox item."""

    title = item.short_summary.strip() or _summarize(item.original_text)
    reflection_questions = generate_reflection_questions(
        [item.to_dict()],
        limit=DEFAULT_REFLECTION_QUESTION_LIMIT,
    )
    lines = [
        f"# {title}",
        "",
        f"- Source: {item.source_channel}",
        f"- Captured: {item.created_at}",
        f"- Type: {item.recommended_type}",
        f"- Destination: {item.recommended_destination}",
    ]
    if item.priority:
        lines.append(f"- Priority: {item.priority}")
    if item.due_date:
        lines.append(f"- Due: {item.due_date}")
    if item.schedule_date:
        lines.append(f"- Scheduled: {item.schedule_date}")
    if item.next_action:
        lines.append(f"- Next action: {item.next_action}")
    if item.linked_project:
        lines.append(f"- Project: [[{item.linked_project}]]")
    if item.linked_knowledge_topic:
        lines.append(f"- Knowledge: [[{item.linked_knowledge_topic}]]")
    if item.blog_candidate:
        lines.append("- Blog candidate: yes")
    if item.report_candidate:
        lines.append("- Report candidate: yes")
    lines.extend(["", "## Daily Reflection", ""])
    lines.extend(f"- {question}" for question in reflection_questions)
    lines.extend(["", "## Original", "", item.original_text.strip(), ""])
    return "\n".join(lines)


def _require_text(field_name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _validate_choice(field_name: str, value: str, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        allowed_values = ", ".join(repr(item) for item in allowed)
        raise ValueError(f"{field_name} must be one of: {allowed_values}")


def _validate_optional_choice(field_name: str, value: str, allowed: tuple[str, ...]) -> None:
    if value:
        _validate_choice(field_name, value, allowed)


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _normalize_original_text(value: Any) -> str:
    """Normalize raw channel content without collapsing intentional newlines."""

    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = "" if value is None else str(value)
    text = text.replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def _normalize_source_channel(value: Any) -> str:
    text = _clean_text(value).lower()
    text = re.sub(r"[^0-9a-z_-]+", "_", text)
    return text.strip("_-") or "unknown"


def _json_field(
    field_type: str,
    description: str,
    *,
    enum: tuple[str, ...] | None = None,
    default: Any = "",
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": field_type, "description": description}
    if enum is not None:
        schema["enum"] = list(enum)
    if default != "" or field_type in {"string", "boolean", "array"}:
        schema["default"] = default
    return schema


PERSONAL_INBOX_SOURCE_METADATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "PersonalInboxSourceMetadata",
    "description": "Normalized metadata describing where a capture originated.",
    "required": ["source_channel", "captured_at"],
    "properties": {
        "source_channel": _json_field("string", "Normalized capture channel such as cli, tui, gateway, slash, cron, or api.", default="unknown"),
        "surface": _json_field("string", "User-facing surface that submitted the capture, if different from channel."),
        "platform": _json_field("string", "Messaging or integration platform such as telegram, discord, slack, webhook, or local."),
        "conversation_id": _json_field("string", "External conversation, chat, or room identifier when available."),
        "thread_id": _json_field("string", "External thread identifier when available."),
        "user_id": _json_field("string", "External user identifier when available."),
        "message_id": _json_field("string", "External message or event identifier when available."),
        "source_url": _json_field("string", "Canonical URL for the source message or document when available."),
        "captured_at": _json_field("string", "UTC timestamp for the capture event."),
        "timezone": _json_field("string", "Source-local timezone name or offset when supplied."),
        "metadata": {"type": "object", "description": "Adapter-specific non-secret metadata preserved for traceability.", "default": {}},
    },
}

PERSONAL_INBOX_CAPTURE_REQUEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "PersonalInboxCaptureRequest",
    "description": "Request contract for capturing raw input into the separate Personal Inbox.",
    "required": ["original_text", "source_metadata"],
    "properties": {
        "original_text": _json_field("string", "Raw captured user input before runtime classification."),
        "text": _json_field("string", "Alias for original_text accepted by tool adapters."),
        "content": _json_field("string", "Alias for original_text accepted by channel adapters."),
        "body": _json_field("string", "Alias for original_text accepted by channel adapters."),
        "message": _json_field("string", "Alias for original_text accepted by channel adapters."),
        "source_channel": _json_field("string", "Convenience alias for source_metadata.source_channel.", default="cli"),
        "channel": _json_field("string", "Top-level alias for source_metadata.source_channel accepted by channel adapters."),
        "source": _json_field("string", "Top-level alias for source_metadata.source_channel accepted by source adapters."),
        "source_metadata": PERSONAL_INBOX_SOURCE_METADATA_SCHEMA,
        "item_id": _json_field("string", "Optional caller-supplied stable item id for idempotent capture."),
        "short_summary": _json_field("string", "Optional editable summary draft; inferred when omitted."),
        "recommended_type": _json_field("string", "Optional editable category draft; inferred when omitted.", enum=ITEM_TYPES),
        "recommended_destination": _json_field("string", "Optional editable Obsidian destination draft; inferred when omitted.", enum=DESTINATIONS),
        "priority": _json_field("string", "Optional editable priority draft.", enum=PRIORITIES),
        "due_date": _json_field("string", "Optional editable due date draft."),
        "schedule_date": _json_field("string", "Optional editable schedule date draft."),
        "next_action": _json_field("string", "Optional editable next action draft."),
        "linked_project": _json_field("string", "Optional editable project link draft."),
        "linked_knowledge_topic": _json_field("string", "Optional editable knowledge topic draft."),
        "blog_candidate": {"type": ["boolean", "null"], "description": "Optional blog candidate draft; inferred when omitted.", "default": None},
        "report_candidate": {"type": ["boolean", "null"], "description": "Optional report candidate draft; inferred when omitted.", "default": None},
        "obsidian_markdown": _json_field("string", "Optional editable Markdown draft for later approved vault write."),
        "vault_write_target": _json_field("string", "Optional editable relative vault write target draft."),
    },
}
for _source_field_name, _source_field_schema in PERSONAL_INBOX_SOURCE_METADATA_SCHEMA["properties"].items():
    if _source_field_name in {"source_channel", "metadata"}:
        continue
    PERSONAL_INBOX_CAPTURE_REQUEST_SCHEMA["properties"].setdefault(
        _source_field_name,
        {
            **_source_field_schema,
            "description": f"Top-level alias for source_metadata.{_source_field_name}.",
        },
    )
del _source_field_name, _source_field_schema

PERSONAL_INBOX_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "PersonalInboxItem",
    "description": "Captured personal input plus editable draft metadata and vault write draft.",
    "required": [
        "item_id",
        "source_channel",
        "original_text",
        "short_summary",
        "recommended_type",
        "recommended_destination",
        "priority",
        "due_date",
        "schedule_date",
        "next_action",
        "linked_project",
        "linked_knowledge_topic",
        "blog_candidate",
        "report_candidate",
        "approval_status",
        "obsidian_markdown",
        "vault_write_target",
    ],
    "properties": {
        "item_id": _json_field("string", "Stable identifier for this Inbox item."),
        "source_channel": _json_field("string", "Capture surface such as cli, tui, gateway, or cron."),
        "original_text": _json_field("string", "Raw captured user input before runtime classification."),
        "short_summary": _json_field("string", "Editable concise runtime-generated summary."),
        "recommended_type": _json_field("string", "Editable draft category for the item.", enum=ITEM_TYPES, default="knowledge note"),
        "recommended_destination": _json_field("string", "Editable draft Obsidian target area.", enum=DESTINATIONS, default="Inbox"),
        "priority": _json_field("string", "Editable priority inferred or supplied during capture.", enum=PRIORITIES),
        "due_date": _json_field("string", "Editable due date, if any."),
        "schedule_date": _json_field("string", "Editable schedule date, if any."),
        "next_action": _json_field("string", "Editable next concrete action, if any."),
        "linked_project": _json_field("string", "Editable related project, if any."),
        "linked_knowledge_topic": _json_field("string", "Editable knowledge topic or note link, if any."),
        "blog_candidate": _json_field("boolean", "Whether this item should be surfaced for a future blog workflow.", default=False),
        "report_candidate": _json_field("boolean", "Whether this item should be surfaced for a future information-report workflow.", default=False),
        "approval_status": _json_field("string", "Per-item review state controlling whether a vault write may proceed.", enum=APPROVAL_STATUSES, default="pending"),
        "obsidian_markdown": _json_field("string", "Editable Markdown draft prepared for approved vault write."),
        "vault_write_target": _json_field("string", "Editable relative vault path or folder target for approved write."),
        "source_metadata": PERSONAL_INBOX_SOURCE_METADATA_SCHEMA,
        "created_at": _json_field("string", "UTC timestamp for initial capture."),
        "updated_at": _json_field("string", "UTC timestamp for the latest draft or review edit."),
        "approved_at": _json_field("string", "UTC timestamp for approval, if any."),
        "reviewed_at": _json_field("string", "UTC timestamp for the latest review decision, if any."),
        "vault_written_at": _json_field("string", "UTC timestamp for Obsidian vault write, if any."),
    },
}

DAILY_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "PersonalInboxDailyReview",
    "description": "Daily review output generated from pending and reviewed Inbox items.",
    "required": [
        "daily_priorities",
        "reflection_questions",
        "candidate_blog_items",
        "candidate_report_items",
    ],
    "properties": {
        "daily_priorities": _json_field("array", "Top priorities for the day.", default=[]),
        "reflection_questions": _json_field("array", "Reflection prompts for daily review.", default=[]),
        "candidate_blog_items": _json_field("array", "Inbox item ids surfaced as blog candidates.", default=[]),
        "candidate_report_items": _json_field("array", "Inbox item ids surfaced as report candidates.", default=[]),
    },
}

PERSONAL_INBOX_CAPTURE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "PersonalInboxCaptureResponse",
    "description": "Response contract returned after a Personal Inbox capture succeeds.",
    "required": [
        "ok",
        "item",
        "source_metadata",
        "editable_draft_fields",
        "approval_required_before_vault_write",
    ],
    "properties": {
        "ok": {"type": "boolean", "description": "Whether capture succeeded.", "default": True},
        "item": PERSONAL_INBOX_ITEM_SCHEMA,
        "source_metadata": PERSONAL_INBOX_SOURCE_METADATA_SCHEMA,
        "editable_draft_fields": {
            "type": "array",
            "description": "Fields callers may show and edit before approval.",
            "items": {"type": "string", "enum": list(EDITABLE_DRAFT_FIELDS)},
            "default": list(EDITABLE_DRAFT_FIELDS),
        },
        "approval_required_before_vault_write": {
            "type": "boolean",
            "description": "Always true for Personal Inbox captures; vault writes require explicit item approval.",
            "default": True,
        },
    },
}


def _coerce_day(day: date | datetime | str | None, tzinfo) -> date:
    if day is None:
        return datetime.now(tzinfo).date()
    if isinstance(day, datetime):
        if day.tzinfo is not None:
            return day.astimezone(tzinfo).date()
        return day.date()
    if isinstance(day, date):
        return day
    text = str(day).strip()
    if not text:
        return datetime.now(tzinfo).date()
    return date.fromisoformat(text[:10])


def _coerce_tz(tz: str | timezone | ZoneInfo | None):
    if tz is None:
        return datetime.now().astimezone().tzinfo or timezone.utc
    if isinstance(tz, str):
        try:
            return ZoneInfo(tz)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {tz}") from exc
    return tz


@dataclass(frozen=True)
class PersonalInboxSourceMetadata:
    """Normalized capture-source metadata shared by all Inbox adapters."""

    source_channel: str = "unknown"
    surface: str = ""
    platform: str = ""
    conversation_id: str = ""
    thread_id: str = ""
    user_id: str = ""
    message_id: str = ""
    source_url: str = ""
    captured_at: str = field(default_factory=_now_utc_iso)
    timezone: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    schema: ClassVar[dict[str, Any]] = PERSONAL_INBOX_SOURCE_METADATA_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_channel", _normalize_source_channel(self.source_channel))
        for field_name in SOURCE_METADATA_FIELDS:
            if field_name == "metadata":
                continue
            object.__setattr__(self, field_name, _clean_text(getattr(self, field_name)))
        object.__setattr__(self, "source_channel", _normalize_source_channel(self.source_channel))
        if not self.captured_at:
            object.__setattr__(self, "captured_at", _now_utc_iso())
        if not isinstance(self.metadata, dict):
            raise ValueError("source metadata must be an object")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None = None, *, source_channel: str = "") -> "PersonalInboxSourceMetadata":
        raw = dict(data or {})
        if source_channel and "source_channel" not in raw:
            raw["source_channel"] = source_channel
        if "source_channel" not in raw:
            raw["source_channel"] = raw.pop("channel", raw.pop("source", "unknown"))
        if "captured_at" not in raw:
            raw["captured_at"] = _now_utc_iso()
        known = {field_name: raw.pop(field_name) for field_name in list(raw) if field_name in SOURCE_METADATA_FIELDS}
        metadata = dict(known.get("metadata") or {})
        metadata.update(raw)
        known["metadata"] = metadata
        return cls(**known)


def normalize_source_metadata(
    source_metadata: "PersonalInboxSourceMetadata | dict[str, Any] | str | None" = None,
    *,
    source_channel: str = "",
) -> PersonalInboxSourceMetadata:
    """Return normalized source metadata from adapter-specific input."""

    if isinstance(source_metadata, PersonalInboxSourceMetadata):
        if source_channel and source_metadata.source_channel != _normalize_source_channel(source_channel):
            return PersonalInboxSourceMetadata.from_dict(source_metadata.to_dict(), source_channel=source_channel)
        return source_metadata
    if isinstance(source_metadata, dict):
        return PersonalInboxSourceMetadata.from_dict(source_metadata, source_channel=source_channel)
    if isinstance(source_metadata, str):
        return PersonalInboxSourceMetadata.from_dict({"source_channel": source_metadata}, source_channel=source_channel)
    return PersonalInboxSourceMetadata.from_dict({}, source_channel=source_channel or "unknown")


@dataclass(frozen=True)
class PersonalInboxCaptureRequest:
    """Capture request with raw input, normalized source metadata, and draft overrides."""

    original_text: str
    source_metadata: PersonalInboxSourceMetadata = field(default_factory=PersonalInboxSourceMetadata)
    item_id: str = ""
    short_summary: str = ""
    recommended_type: str = ""
    recommended_destination: str = ""
    priority: str = ""
    due_date: str = ""
    schedule_date: str = ""
    next_action: str = ""
    linked_project: str = ""
    linked_knowledge_topic: str = ""
    blog_candidate: bool | None = None
    report_candidate: bool | None = None
    obsidian_markdown: str = ""
    vault_write_target: str = ""

    schema: ClassVar[dict[str, Any]] = PERSONAL_INBOX_CAPTURE_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "original_text", _normalize_original_text(self.original_text))
        _require_text("original_text", self.original_text)
        object.__setattr__(self, "source_metadata", normalize_source_metadata(self.source_metadata))
        _validate_optional_choice("recommended_type", self.recommended_type, ITEM_TYPES)
        _validate_optional_choice("recommended_destination", self.recommended_destination, DESTINATIONS)
        _validate_choice("priority", self.priority, PRIORITIES)
        if self.blog_candidate is not None and not isinstance(self.blog_candidate, bool):
            raise ValueError("blog_candidate must be a boolean or null")
        if self.report_candidate is not None and not isinstance(self.report_candidate, bool):
            raise ValueError("report_candidate must be a boolean or null")

    @property
    def source_channel(self) -> str:
        return self.source_metadata.source_channel

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PersonalInboxCaptureRequest":
        raw = dict(data)
        text_values: list[Any] = []
        for field_name in RAW_TEXT_FIELDS:
            if field_name in raw:
                text_values.append(raw.pop(field_name))
        original_text = next(
            (value for value in text_values if _normalize_original_text(value)),
            text_values[0] if text_values else "",
        )
        source_channel = raw.pop("source_channel", "") or raw.pop("channel", "") or raw.pop("source", "")
        raw_source_metadata = raw.pop("source_metadata", None)
        top_level_source_metadata = {
            field_name: raw.pop(field_name)
            for field_name in list(raw)
            if field_name in SOURCE_METADATA_FIELDS
        }
        if isinstance(raw_source_metadata, dict):
            source_metadata_payload = {**top_level_source_metadata, **raw_source_metadata}
        elif top_level_source_metadata:
            source_metadata_payload = top_level_source_metadata
            if raw_source_metadata:
                source_metadata_payload.setdefault("source_channel", raw_source_metadata)
        else:
            source_metadata_payload = raw_source_metadata
        if source_channel and isinstance(source_metadata_payload, dict):
            source_metadata_payload["source_channel"] = source_channel
        source_metadata = normalize_source_metadata(source_metadata_payload, source_channel=source_channel)
        allowed = {
            "item_id",
            "short_summary",
            "recommended_type",
            "recommended_destination",
            "priority",
            "due_date",
            "schedule_date",
            "next_action",
            "linked_project",
            "linked_knowledge_topic",
            "blog_candidate",
            "report_candidate",
            "obsidian_markdown",
            "vault_write_target",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(f"Unsupported personal Inbox capture fields: {', '.join(unknown)}")
        return cls(original_text=original_text, source_metadata=source_metadata, **raw)


@dataclass(frozen=True)
class PersonalInboxItem:
    """Serializable Inbox item with editable draft metadata.

    ``obsidian_markdown`` and ``vault_write_target`` are only drafts here. The
    actual vault writer must check ``is_ready_for_vault_write`` before writing.
    """

    item_id: str
    source_channel: str
    original_text: str
    short_summary: str = ""
    recommended_type: str = "knowledge note"
    recommended_destination: str = "Inbox"
    priority: str = ""
    due_date: str = ""
    schedule_date: str = ""
    next_action: str = ""
    linked_project: str = ""
    linked_knowledge_topic: str = ""
    blog_candidate: bool = False
    report_candidate: bool = False
    approval_status: str = "pending"
    obsidian_markdown: str = ""
    vault_write_target: str = ""
    source_metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now_utc_iso)
    updated_at: str = field(default_factory=_now_utc_iso)
    approved_at: str = ""
    reviewed_at: str = ""
    vault_written_at: str = ""

    schema: ClassVar[dict[str, Any]] = PERSONAL_INBOX_ITEM_SCHEMA

    def __post_init__(self) -> None:
        _require_text("item_id", self.item_id)
        _require_text("source_channel", self.source_channel)
        _require_text("original_text", self.original_text)
        _validate_choice("recommended_type", self.recommended_type, ITEM_TYPES)
        _validate_choice("recommended_destination", self.recommended_destination, DESTINATIONS)
        _validate_choice("priority", self.priority, PRIORITIES)
        _validate_choice("approval_status", self.approval_status, APPROVAL_STATUSES)
        if not isinstance(self.blog_candidate, bool):
            raise ValueError("blog_candidate must be a boolean")
        if not isinstance(self.report_candidate, bool):
            raise ValueError("report_candidate must be a boolean")
        if not isinstance(self.source_metadata, dict):
            raise ValueError("source_metadata must be an object")

    @property
    def is_ready_for_vault_write(self) -> bool:
        return (
            self.approval_status == APPROVED_STATUS
            and bool(self.obsidian_markdown.strip())
            and bool(self.vault_write_target.strip())
        )

    def apply_review_edits(self, **edits: Any) -> "PersonalInboxItem":
        unknown = sorted(
            set(edits)
            - set(EDITABLE_DRAFT_FIELDS)
            - set(INTERNAL_REVIEW_FIELDS)
            - {"approval_status"}
        )
        if unknown:
            raise ValueError(f"Unsupported personal Inbox edit fields: {', '.join(unknown)}")
        if "approval_status" not in edits and self.approval_status == "pending":
            edits["approval_status"] = "edited"
        edits["updated_at"] = _now_utc_iso()
        return replace(self, **edits)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PersonalInboxItem":
        return cls(**_migrate_item_payload(data))


def _migrated_text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(part).strip() for part in value if str(part).strip())
    return str(value).strip()


def _migrated_candidate_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _migrate_item_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Return an item payload with the v1 linked/candidate fields populated."""

    migrated = dict(data)
    migrated["linked_project"] = _migrated_text_value(migrated.get("linked_project", ""))

    knowledge_topic = _migrated_text_value(migrated.get("linked_knowledge_topic", ""))
    for legacy_field in ("linked_knowledge_notes", "related_notes", "related_note", "knowledge_topic"):
        legacy_value = migrated.pop(legacy_field, None)
        if not knowledge_topic:
            knowledge_topic = _migrated_text_value(legacy_value)
    migrated["linked_knowledge_topic"] = knowledge_topic

    migrated["blog_candidate"] = _migrated_candidate_flag(migrated.get("blog_candidate", False))
    migrated["report_candidate"] = _migrated_candidate_flag(migrated.get("report_candidate", False))
    return migrated


@dataclass(frozen=True)
class PersonalInboxCaptureResponse:
    """Capture response exposing the item and editable draft contract."""

    item: PersonalInboxItem
    source_metadata: PersonalInboxSourceMetadata
    ok: bool = True
    editable_draft_fields: list[str] = field(default_factory=lambda: list(EDITABLE_DRAFT_FIELDS))
    approval_required_before_vault_write: bool = True

    schema: ClassVar[dict[str, Any]] = PERSONAL_INBOX_CAPTURE_RESPONSE_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "item": self.item.to_dict(),
            "source_metadata": self.source_metadata.to_dict(),
            "editable_draft_fields": list(self.editable_draft_fields),
            "approval_required_before_vault_write": self.approval_required_before_vault_write,
        }


@dataclass(frozen=True)
class PersonalInboxDailyReview:
    """Daily review aggregate generated from Inbox items."""

    daily_priorities: list[str] = field(default_factory=list)
    reflection_questions: list[str] = field(default_factory=list)
    candidate_blog_items: list[str] = field(default_factory=list)
    candidate_report_items: list[str] = field(default_factory=list)

    schema: ClassVar[dict[str, Any]] = DAILY_REVIEW_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PersonalInboxDailyReview":
        return cls(
            daily_priorities=list(data.get("daily_priorities") or []),
            reflection_questions=list(data.get("reflection_questions") or []),
            candidate_blog_items=list(data.get("candidate_blog_items") or []),
            candidate_report_items=list(data.get("candidate_report_items") or []),
        )


class PersonalInboxRoutingService:
    """Shared channel payload router for Personal Inbox captures.

    The router is intentionally persistence-free and vault-free.  Thin adapters
    from CLI, TUI, gateway, cron, API, or plugins can pass their native payload
    shape here and receive the canonical pending Inbox item draft.
    """

    def normalize_payload(
        self,
        payload: PersonalInboxCaptureRequest | dict[str, Any] | str | bytes,
        **overrides: Any,
    ) -> PersonalInboxCaptureRequest:
        """Coerce adapter-specific capture payload into a canonical request."""

        if isinstance(payload, PersonalInboxCaptureRequest):
            request_data = payload.to_dict()
        elif isinstance(payload, dict):
            request_data = dict(payload)
        else:
            request_data = {"original_text": payload}
        request_data.update(overrides)
        return PersonalInboxCaptureRequest.from_dict(request_data)

    def route_to_item(
        self,
        payload: PersonalInboxCaptureRequest | dict[str, Any] | str | bytes,
        **overrides: Any,
    ) -> PersonalInboxItem:
        """Normalize a channel payload into a pending Inbox item record."""

        request = self.normalize_payload(payload, **overrides)
        item_type = request.recommended_type or _infer_type(request.original_text)
        destination = request.recommended_destination or _destination_for_type(item_type)
        captured_at = request.source_metadata.captured_at
        return PersonalInboxItem(
            item_id=request.item_id or _new_item_id(),
            source_channel=request.source_channel,
            original_text=request.original_text,
            short_summary=request.short_summary or _summarize(request.original_text),
            recommended_type=item_type,
            recommended_destination=destination,
            priority=request.priority,
            due_date=request.due_date,
            schedule_date=request.schedule_date,
            next_action=request.next_action,
            linked_project=request.linked_project,
            linked_knowledge_topic=request.linked_knowledge_topic,
            blog_candidate=item_type == "blog candidate" if request.blog_candidate is None else request.blog_candidate,
            report_candidate=item_type == "report candidate" if request.report_candidate is None else request.report_candidate,
            obsidian_markdown=request.obsidian_markdown,
            vault_write_target=request.vault_write_target,
            source_metadata=request.source_metadata.to_dict(),
            created_at=captured_at,
            updated_at=captured_at,
        )

    def route(
        self,
        payload: PersonalInboxCaptureRequest | dict[str, Any] | str | bytes,
        **overrides: Any,
    ) -> tuple[PersonalInboxCaptureRequest, PersonalInboxItem]:
        """Return both normalized request metadata and the routed item record."""

        request = self.normalize_payload(payload, **overrides)
        return request, self.route_to_item(request)


class PersonalInboxCaptureService:
    """Channel-agnostic capture normalizer and draft item builder.

    This service has no persistence or vault access.  CLI, TUI, gateway, cron,
    API, and plugin adapters can hand it raw channel payloads; it validates the
    content, normalizes source metadata, infers editable draft fields, and
    returns a pending Inbox item response for the store to persist.
    """

    def normalize_request(
        self,
        request: PersonalInboxCaptureRequest | dict[str, Any] | str | bytes,
        **overrides: Any,
    ) -> PersonalInboxCaptureRequest:
        """Coerce adapter-specific capture input into the canonical request."""

        return PersonalInboxRoutingService().normalize_payload(request, **overrides)

    def build_item(self, request: PersonalInboxCaptureRequest) -> PersonalInboxItem:
        """Build a pending Inbox item with normalized text and draft metadata."""

        return PersonalInboxRoutingService().route_to_item(request)

    def capture(
        self,
        request: PersonalInboxCaptureRequest | dict[str, Any] | str | bytes,
        **overrides: Any,
    ) -> PersonalInboxCaptureResponse:
        """Validate and normalize raw capture input into a response contract."""

        capture_request = self.normalize_request(request, **overrides)
        item = self.build_item(capture_request)
        return PersonalInboxCaptureResponse(item=item, source_metadata=capture_request.source_metadata)


def capture_personal_inbox_item(
    request: PersonalInboxCaptureRequest | dict[str, Any] | str | bytes,
    *,
    inbox_path: str | Path | None = None,
    store: "PersonalInboxStore | None" = None,
    **overrides: Any,
) -> PersonalInboxCaptureResponse:
    """Public capture callable for thin CLI, TUI, gateway, API, and plugin adapters.

    The callable persists into the separate Personal Inbox store through the
    shared capture service path and never writes to the Obsidian vault.
    """

    target_store = store if store is not None else PersonalInboxStore(inbox_path)
    return target_store.capture_item(request, **overrides)


def _parse_timestamp(value: Any, tzinfo) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed_day = date.fromisoformat(text[:10])
            except ValueError:
                return None
            return datetime.combine(parsed_day, datetime.min.time(), tzinfo=tzinfo)

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tzinfo)
    return parsed.astimezone(tzinfo)


def _review_day(item: dict[str, Any], tzinfo) -> date | None:
    for field in _REVIEW_DATE_FIELDS:
        parsed = _parse_timestamp(item.get(field), tzinfo)
        if parsed is not None:
            return parsed.date()
    return None


def _priority_rank(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)

    text = str(value).strip().lower()
    if not text:
        return 0
    aliases = {
        "urgent": 100,
        "critical": 100,
        "p0": 100,
        "high": 75,
        "p1": 75,
        "medium": 50,
        "normal": 50,
        "p2": 50,
        "low": 25,
        "p3": 25,
        "none": 0,
    }
    if text in aliases:
        return aliases[text]
    try:
        return int(float(text))
    except ValueError:
        return 0


def _stable_review_sort_key(item: dict[str, Any]) -> tuple[int, str, str]:
    rank = item.get("priority_rank")
    if rank in (None, ""):
        rank = item.get("priority")
    return (
        -_priority_rank(rank),
        str(item.get("due_date") or ""),
        str(item.get("schedule_date") or ""),
        str(item.get("item_id") or ""),
    )


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _reflection_item(item: dict[str, Any]) -> dict[str, Any]:
    """Return the approved item shape needed by reflection generation."""

    result = {
        "item_id": str(item.get("item_id") or item.get("id") or ""),
        "source_channel": item.get("source_channel", ""),
        "original_text": item.get("original_text", ""),
        "short_summary": item.get("short_summary", ""),
        "recommended_type": item.get("recommended_type", ""),
        "recommended_destination": item.get("recommended_destination", ""),
        "approval_status": APPROVED_STATUS,
        "vault_write_target": item.get("vault_write_target", ""),
        "approved_at": item.get("approved_at", ""),
    }
    for field in PRIORITY_FIELDS:
        result[field] = _as_bool(item.get(field)) if field.endswith("_candidate") else item.get(field, "")

    result["priority_rank"] = _priority_rank(result.get("priority"))
    result["priority_metadata"] = {
        field: result[field]
        for field in PRIORITY_FIELDS
    }
    result["priority_metadata"]["priority_rank"] = result["priority_rank"]
    return result


def _clean_inline_text(value: Any, *, fallback: str = "", max_length: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        text = fallback
    if max_length > 3 and len(text) > max_length:
        return f"{text[: max_length - 1].rstrip()}..."
    return text


def _item_label(item: dict[str, Any]) -> str:
    return _clean_inline_text(
        item.get("short_summary") or item.get("original_text") or item.get("item_id"),
        fallback="approved item",
    )


def _priority_label(item: dict[str, Any]) -> str:
    priority = str(item.get("priority") or "").strip().lower()
    return priority if priority and priority != "none" else "unranked"


def _priority_signal_parts(item: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    priority = _priority_label(item)
    if priority != "unranked":
        parts.append(f"{priority} priority")
    if item.get("due_date"):
        parts.append(f"due {item['due_date']}")
    if item.get("schedule_date"):
        parts.append(f"scheduled {item['schedule_date']}")
    if item.get("next_action"):
        parts.append(f"next action: {_clean_inline_text(item['next_action'], max_length=80)}")
    if item.get("linked_project"):
        parts.append(f"project: {_clean_inline_text(item['linked_project'], max_length=60)}")
    if item.get("linked_knowledge_topic"):
        parts.append(f"topic: {_clean_inline_text(item['linked_knowledge_topic'], max_length=60)}")
    if _as_bool(item.get("blog_candidate")):
        parts.append("blog candidate")
    if _as_bool(item.get("report_candidate")):
        parts.append("report candidate")
    return parts


def _priority_signal_text(item: dict[str, Any]) -> str:
    parts = _priority_signal_parts(item)
    return ", ".join(parts) if parts else "no explicit priority signals"


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _daily_priority_line(item: dict[str, Any]) -> str:
    label = _item_label(item)
    priority = _priority_label(item).title()
    action = _clean_inline_text(item.get("next_action"), max_length=80)
    suffix_parts = []
    if item.get("due_date"):
        suffix_parts.append(f"due {item['due_date']}")
    if item.get("schedule_date"):
        suffix_parts.append(f"scheduled {item['schedule_date']}")
    if item.get("linked_project"):
        suffix_parts.append(f"project {_clean_inline_text(item['linked_project'], max_length=60)}")
    if item.get("linked_knowledge_topic"):
        suffix_parts.append(f"topic {_clean_inline_text(item['linked_knowledge_topic'], max_length=60)}")

    line = f"{priority}: {label}"
    if action:
        line = f"{line} -> {action}"
    if suffix_parts:
        line = f"{line} ({', '.join(suffix_parts)})"
    return line


def _candidate_item_ids(items: list[dict[str, Any]], field_name: str) -> list[str]:
    return [
        str(item["item_id"])
        for item in items
        if item.get("item_id") and _as_bool(item.get(field_name))
    ]


def generate_reflection_questions(
    approved_items: list[dict[str, Any]],
    *,
    limit: int = DEFAULT_REFLECTION_QUESTION_LIMIT,
) -> list[str]:
    """Generate deterministic reflection questions from approved items.

    The output is intentionally model-free.  Questions are derived from approved
    item content plus priority metadata so the daily review stays stable across
    CLI, TUI, gateway, and scheduled callers.
    """

    if limit <= 0:
        return []

    items = sorted(approved_items, key=_stable_review_sort_key)
    if not items:
        return [
            "What mattered most today, and what should be captured or approved tomorrow?",
            "Which unresolved commitment should become the first priority in the next review?",
            "What context is missing from the personal Inbox before it can be trusted for planning?",
        ][:limit]

    questions: list[str] = []
    top_item = items[0]
    top_label = _item_label(top_item)
    _append_unique(
        questions,
        f"How did '{top_label}' move forward today, and what should happen next? "
        f"Signals: {_priority_signal_text(top_item)}.",
    )

    high_items = [
        item
        for item in items
        if _priority_rank(item.get("priority_rank") or item.get("priority")) >= _priority_rank("high")
    ]
    if high_items:
        high_labels = ", ".join(f"'{_item_label(item)}'" for item in high_items[:3])
        _append_unique(
            questions,
            f"Which high-priority commitment needs protected focus tomorrow: {high_labels}? "
            f"Signals: {_priority_signal_text(high_items[0])}.",
        )

    due_items = [item for item in items if item.get("due_date")]
    if due_items:
        due_item = due_items[0]
        _append_unique(
            questions,
            f"What risk or dependency could block '{_item_label(due_item)}' before {due_item['due_date']}?",
        )

    scheduled_items = [item for item in items if item.get("schedule_date")]
    if scheduled_items:
        scheduled_item = scheduled_items[0]
        _append_unique(
            questions,
            f"What needs to be prepared before '{_item_label(scheduled_item)}' on {scheduled_item['schedule_date']}?",
        )

    project_items = [item for item in items if item.get("linked_project")]
    if project_items:
        project_item = project_items[0]
        project = _clean_inline_text(project_item["linked_project"], max_length=80)
        _append_unique(
            questions,
            f"What did today's approved work reveal about project '{project}', especially '{_item_label(project_item)}'?",
        )

    topic_items = [item for item in items if item.get("linked_knowledge_topic")]
    if topic_items:
        topic_item = topic_items[0]
        topic = _clean_inline_text(topic_item["linked_knowledge_topic"], max_length=80)
        _append_unique(
            questions,
            f"What should be clarified, connected, or updated in knowledge topic '{topic}' after '{_item_label(topic_item)}'?",
        )

    blog_items = [item for item in items if _as_bool(item.get("blog_candidate"))]
    if blog_items:
        _append_unique(
            questions,
            f"Which approved insight is worth developing into a blog draft: '{_item_label(blog_items[0])}'?",
        )

    report_items = [item for item in items if _as_bool(item.get("report_candidate"))]
    if report_items:
        _append_unique(
            questions,
            f"Which approved item should seed a future information report: '{_item_label(report_items[0])}'?",
        )

    _append_unique(
        questions,
        f"What can be deferred, simplified, or delegated after reviewing {len(items)} approved item(s) today?",
    )
    return questions[:limit]


def build_daily_review_from_items(
    approved_items: list[dict[str, Any]],
    *,
    max_priorities: int = DEFAULT_DAILY_PRIORITY_LIMIT,
    max_questions: int = DEFAULT_REFLECTION_QUESTION_LIMIT,
) -> PersonalInboxDailyReview:
    """Build daily priorities, reflection prompts, and candidate lists."""

    items = sorted(approved_items, key=_stable_review_sort_key)
    daily_priorities = [
        _daily_priority_line(item)
        for item in items[:max(0, max_priorities)]
    ]
    return PersonalInboxDailyReview(
        daily_priorities=daily_priorities,
        reflection_questions=generate_reflection_questions(items, limit=max_questions),
        candidate_blog_items=_candidate_item_ids(items, "blog_candidate"),
        candidate_report_items=_candidate_item_ids(items, "report_candidate"),
    )


class PersonalInboxStore:
    """JSON-backed Personal Inbox store.

    The expected on-disk shape is either a list of item objects or a dict with an
    ``items`` list.  This leniency keeps the retrieval path compatible with early
    MVP writers while keeping approval filtering centralized here.
    """

    store_kind: ClassVar[str] = INBOX_STORE_KIND

    def __init__(self, path: str | Path | None = None):
        raw_path = Path(path) if path is not None else default_inbox_path()
        _validate_inbox_store_path(raw_path)
        self.path = raw_path

    @property
    def is_isolated_from_session_and_memory(self) -> bool:
        return inbox_store_path_is_isolated(self.path)

    def _read_payload(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"items": [], "daily_reviews": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {"items": [item for item in data if isinstance(item, dict)], "daily_reviews": {}}
        if not isinstance(data, dict):
            return {"items": [], "daily_reviews": {}}

        items = data.get("items", [])
        daily_reviews = data.get("daily_reviews", {})
        return {
            **data,
            "items": [item for item in items if isinstance(item, dict)] if isinstance(items, list) else [],
            "daily_reviews": daily_reviews if isinstance(daily_reviews, dict) else {},
        }

    def _write_payload(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.path)

    def read_items(self) -> list[dict[str, Any]]:
        """Read inbox items from disk. Missing inbox files are empty."""

        return [_migrate_item_payload(item) for item in self._read_payload()["items"]]

    def write_items(self, items: list[dict[str, Any]]) -> None:
        """Write inbox items for tests and thin adapters."""

        payload = self._read_payload()
        payload["items"] = [_migrate_item_payload(item) for item in items if isinstance(item, dict)]
        self._write_payload(payload)

    def _reflection_questions_for_day(
        self,
        day: date,
        approved_items: list[dict[str, Any]],
        *,
        max_questions: int,
    ) -> list[str]:
        """Return one persisted reflection-question set per review day."""

        payload = self._read_payload()
        daily_reviews = payload.setdefault("daily_reviews", {})
        review_key = day.isoformat()
        existing = daily_reviews.get(review_key)
        if isinstance(existing, dict) and isinstance(existing.get("reflection_questions"), list):
            return [str(question) for question in existing["reflection_questions"]][:max(0, max_questions)]

        questions = generate_reflection_questions(approved_items, limit=max_questions)
        daily_reviews[review_key] = {
            "created_at": _now_utc_iso(),
            "reflection_questions": questions,
        }
        self._write_payload(payload)
        return questions

    def create_item(
        self,
        original_text: str,
        *,
        source_channel: str = "cli",
        source_metadata: PersonalInboxSourceMetadata | dict[str, Any] | str | None = None,
        short_summary: str = "",
        recommended_type: str = "",
        recommended_destination: str = "",
        priority: str = "",
        due_date: str = "",
        schedule_date: str = "",
        next_action: str = "",
        linked_project: str = "",
        linked_knowledge_topic: str = "",
        blog_candidate: bool | None = None,
        report_candidate: bool | None = None,
        obsidian_markdown: str = "",
        vault_write_target: str = "",
        item_id: str | None = None,
    ) -> PersonalInboxItem:
        """Create a captured Inbox item with a stable ID and timestamps."""

        request = PersonalInboxCaptureRequest(
            original_text=original_text,
            source_metadata=normalize_source_metadata(source_metadata, source_channel=source_channel),
            item_id=item_id or "",
            short_summary=short_summary,
            recommended_type=recommended_type,
            recommended_destination=recommended_destination,
            priority=priority,
            due_date=due_date,
            schedule_date=schedule_date,
            next_action=next_action,
            linked_project=linked_project,
            linked_knowledge_topic=linked_knowledge_topic,
            blog_candidate=blog_candidate,
            report_candidate=report_candidate,
            obsidian_markdown=obsidian_markdown,
            vault_write_target=vault_write_target,
        )
        return self.capture_item(request).item

    def capture_item(
        self,
        request: PersonalInboxCaptureRequest | dict[str, Any] | str | bytes,
        **overrides: Any,
    ) -> PersonalInboxCaptureResponse:
        """Capture a request and return the normalized response contract."""

        response = PersonalInboxCaptureService().capture(request, **overrides)
        item = response.item
        items = self.read_items()
        items.append(item.to_dict())
        self.write_items(items)
        return response

    def add_item(
        self,
        original_text: str,
        *,
        source_channel: str = "cli",
        short_summary: str = "",
        recommended_type: str = "",
        recommended_destination: str = "",
    ) -> PersonalInboxItem:
        """Capture raw text as a pending Inbox item."""

        return self.create_item(
            original_text,
            source_channel=source_channel,
            short_summary=short_summary,
            recommended_type=recommended_type,
            recommended_destination=recommended_destination,
        )

    def get_item(self, item_id: str) -> PersonalInboxItem:
        for item in self.read_items():
            if str(item.get("item_id")) == item_id:
                return PersonalInboxItem.from_dict(item)
        raise KeyError(f"Inbox item not found: {item_id}")

    def read_item(self, item_id: str) -> PersonalInboxItem:
        """Read one captured Inbox item by stable ID."""

        return self.get_item(item_id)

    def list_items(self, *, statuses: tuple[str, ...] | None = None) -> list[PersonalInboxItem]:
        allowed = statuses or ("pending", "edited", "deferred")
        return [
            PersonalInboxItem.from_dict(item)
            for item in self.read_items()
            if str(item.get("approval_status", "pending")) in allowed
        ]

    def update_item(self, item_id: str, **edits: Any) -> PersonalInboxItem:
        items = self.read_items()
        updated: PersonalInboxItem | None = None
        for index, item in enumerate(items):
            if str(item.get("item_id")) != item_id:
                continue
            updated = PersonalInboxItem.from_dict(item).apply_review_edits(**edits)
            items[index] = updated.to_dict()
            break
        if updated is None:
            raise KeyError(f"Inbox item not found: {item_id}")
        self.write_items(items)
        return updated

    def decide_item(self, item_id: str, decision: str) -> PersonalInboxItem:
        decision = decision.strip().lower()
        if decision not in {"approve", "approved", "defer", "deferred", "archive", "archived", "freeze", "frozen", "trash"}:
            raise ValueError("decision must be approve, defer, archive, freeze, or trash")
        status = {
            "approve": APPROVED_STATUS,
            "approved": APPROVED_STATUS,
            "defer": "deferred",
            "deferred": "deferred",
            "archive": "archived",
            "archived": "archived",
            "freeze": "frozen",
            "frozen": "frozen",
            "trash": "trash",
        }[decision]
        edits: dict[str, Any] = {"approval_status": status, "reviewed_at": _now_utc_iso()}
        item = self.get_item(item_id)
        if status == APPROVED_STATUS:
            edits["approved_at"] = _now_utc_iso()
            edits["obsidian_markdown"] = item.obsidian_markdown or build_obsidian_markdown(item)
            edits["vault_write_target"] = item.vault_write_target or _default_vault_target(item)
        return self.update_item(item_id, **edits)

    def write_approved_item_to_vault(
        self,
        item_id: str,
        *,
        vault_path: str | Path | None = None,
    ) -> Path:
        """Write an approved item to Obsidian Markdown, never before approval."""

        item = self.get_item(item_id)
        if item.approval_status != APPROVED_STATUS:
            raise ValueError("Inbox item must be approved before writing to Obsidian")
        if not item.obsidian_markdown.strip() or not item.vault_write_target.strip():
            item = self.decide_item(item_id, "approve")
        root = Path(vault_path).expanduser() if vault_path is not None else configured_obsidian_vault_path()
        if root is None:
            raise ValueError("Obsidian vault path is not configured")
        root = root.resolve(strict=False)
        target = (root / item.vault_write_target).resolve(strict=False)
        if not _is_relative_to(target, root):
            raise ValueError("vault_write_target must stay inside the Obsidian vault")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing = target.read_text(encoding="utf-8")
            content = f"{existing.rstrip()}\n\n---\n\n{item.obsidian_markdown.strip()}\n"
        else:
            content = f"{item.obsidian_markdown.strip()}\n"
        target.write_text(content, encoding="utf-8")
        self.update_item(item_id, vault_written_at=_now_utc_iso())
        return target

    def build_active_review(self) -> PersonalInboxDailyReview:
        """Build a lightweight review from active, not-yet-approved items."""

        active = self.list_items()
        priority_items = [
            item for item in active
            if item.priority in {"urgent", "high"} or item.next_action.strip()
        ]
        if not priority_items:
            priority_items = active[:1]
        return PersonalInboxDailyReview(
            daily_priorities=[
                item.next_action or item.short_summary or item.original_text
                for item in priority_items[:3]
            ],
            reflection_questions=[
                "Which captured thought is not actually a task?",
                "What should be frozen so it stops occupying attention?",
                "What is the one main output for tomorrow?",
            ],
            candidate_blog_items=[item.item_id for item in active if item.blog_candidate],
            candidate_report_items=[item.item_id for item in active if item.report_candidate],
        )

    def list_approved_items_for_day(
        self,
        day: date | datetime | str | None = None,
        *,
        tz: str | timezone | ZoneInfo | None = None,
    ) -> list[dict[str, Any]]:
        """Return approved items reviewed on ``day`` with priority metadata.

        Only ``approval_status == "approved"`` items are returned.  Deferred,
        pending, edited, or archived items are excluded so reflection generation
        cannot accidentally treat unapproved inbox contents as approved work.
        """

        tzinfo = _coerce_tz(tz)
        target_day = _coerce_day(day, tzinfo)
        approved: list[dict[str, Any]] = []
        for item in self.read_items():
            status = str(item.get("approval_status", "")).strip().lower()
            if status != APPROVED_STATUS:
                continue
            if _review_day(item, tzinfo) != target_day:
                continue
            approved.append(_reflection_item(item))

        return sorted(
            approved,
            key=lambda item: (
                -item["priority_rank"],
                str(item.get("due_date") or ""),
                str(item.get("schedule_date") or ""),
                item["item_id"],
            ),
        )

    def build_daily_review(
        self,
        day: date | datetime | str | None = None,
        *,
        tz: str | timezone | ZoneInfo | None = None,
        max_priorities: int = DEFAULT_DAILY_PRIORITY_LIMIT,
        max_questions: int = DEFAULT_REFLECTION_QUESTION_LIMIT,
    ) -> PersonalInboxDailyReview:
        """Build deterministic daily review output from approved items."""

        tzinfo = _coerce_tz(tz)
        target_day = _coerce_day(day, tzinfo)
        approved_items = self.list_approved_items_for_day(target_day, tz=tzinfo)
        review = build_daily_review_from_items(
            approved_items,
            max_priorities=max_priorities,
            max_questions=max_questions,
        )
        return replace(
            review,
            reflection_questions=self._reflection_questions_for_day(
                target_day,
                approved_items,
                max_questions=max_questions,
            ),
        )


def list_approved_items_for_day(
    day: date | datetime | str | None = None,
    *,
    inbox_path: str | Path | None = None,
    tz: str | timezone | ZoneInfo | None = None,
) -> list[dict[str, Any]]:
    """Convenience wrapper for reflection-generation callers."""

    return PersonalInboxStore(inbox_path).list_approved_items_for_day(day, tz=tz)


def build_daily_review(
    day: date | datetime | str | None = None,
    *,
    inbox_path: str | Path | None = None,
    tz: str | timezone | ZoneInfo | None = None,
    max_priorities: int = DEFAULT_DAILY_PRIORITY_LIMIT,
    max_questions: int = DEFAULT_REFLECTION_QUESTION_LIMIT,
) -> PersonalInboxDailyReview:
    """Convenience wrapper for deterministic daily review generation."""

    return PersonalInboxStore(inbox_path).build_daily_review(
        day,
        tz=tz,
        max_priorities=max_priorities,
        max_questions=max_questions,
    )


PersonalInboxRepository = PersonalInboxStore
