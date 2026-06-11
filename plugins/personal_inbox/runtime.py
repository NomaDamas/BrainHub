"""Embeddable Personal Inbox runtime for host plugins and gateways.

This module is the stable integration surface for loading Personal Inbox into
an agent host without copying command or storage logic into each host surface.
"""

from __future__ import annotations

import shlex
from datetime import datetime
from typing import Any, Callable

import brainhub.personal_inbox as personal_inbox
from brainhub.personal_inbox import (
    EDITABLE_DRAFT_FIELDS,
    PERSONAL_INBOX_CAPTURE_REQUEST_SCHEMA,
    PersonalInboxCaptureResponse,
    PersonalInboxDailyReview,
    PersonalInboxStore,
)

CaptureFunc = Callable[..., PersonalInboxCaptureResponse]
StoreFactory = Callable[[], PersonalInboxStore]


def item_payload(item: Any) -> dict[str, Any]:
    return item.to_dict() if hasattr(item, "to_dict") else dict(item)


def format_item(item: Any) -> str:
    payload = item_payload(item)
    return (
        f"{payload['item_id']} [{payload['approval_status']}] "
        f"{payload['short_summary']} -> {payload['recommended_destination']} "
        f"({payload['recommended_type']})"
    )


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def combined_review(
    active_review: PersonalInboxDailyReview,
    daily_review: PersonalInboxDailyReview,
) -> PersonalInboxDailyReview:
    return PersonalInboxDailyReview(
        daily_priorities=dedupe(active_review.daily_priorities + daily_review.daily_priorities),
        reflection_questions=dedupe(active_review.reflection_questions + daily_review.reflection_questions),
        candidate_blog_items=dedupe(active_review.candidate_blog_items + daily_review.candidate_blog_items),
        candidate_report_items=dedupe(active_review.candidate_report_items + daily_review.candidate_report_items),
    )


def gateway_event_source_metadata(
    event: Any,
    *,
    source_channel: str,
    surface: str,
) -> dict[str, Any]:
    """Build Personal Inbox source metadata from a host gateway event."""

    source = event.source
    platform_value = getattr(getattr(source, "platform", None), "value", "")
    timestamp = getattr(event, "timestamp", None)
    if isinstance(timestamp, datetime):
        captured_at = (
            timestamp.astimezone().isoformat()
            if timestamp.tzinfo
            else timestamp.isoformat()
        )
    else:
        captured_at = ""
    message_type = getattr(getattr(event, "message_type", None), "value", "")
    return {
        "source_channel": source_channel,
        "surface": surface,
        "platform": platform_value,
        "conversation_id": getattr(source, "chat_id", "") or "",
        "thread_id": getattr(source, "thread_id", "") or "",
        "user_id": getattr(source, "user_id", "") or "",
        "message_id": event.message_id or getattr(source, "message_id", "") or "",
        "captured_at": captured_at,
        "metadata": {
            "chat_name": getattr(source, "chat_name", "") or "",
            "chat_type": getattr(source, "chat_type", "") or "",
            "user_name": getattr(source, "user_name", "") or "",
            "message_type": message_type,
        },
    }


class PersonalInboxRuntime:
    """Reusable Personal Inbox API for embedded modules."""

    capture_request_schema = PERSONAL_INBOX_CAPTURE_REQUEST_SCHEMA
    editable_draft_fields = EDITABLE_DRAFT_FIELDS

    def __init__(
        self,
        *,
        store_factory: StoreFactory | None = None,
        capture_func: CaptureFunc | None = None,
    ) -> None:
        self._store_factory = store_factory or PersonalInboxStore
        self._capture_func = capture_func

    def store(self) -> PersonalInboxStore:
        return self._store_factory()

    def capture(self, request: dict[str, Any] | str | bytes, **kwargs: Any) -> PersonalInboxCaptureResponse:
        capture_func = self._capture_func or personal_inbox.capture_personal_inbox_item
        return capture_func(request, **kwargs)

    def list_items(self, *, statuses: tuple[str, ...] | None = None) -> list[Any]:
        return self.store().list_items(statuses=statuses)

    def read_item(self, item_id: str) -> Any:
        return self.store().read_item(item_id)

    def update_item(self, item_id: str, **edits: Any) -> Any:
        return self.store().update_item(item_id, **edits)

    def decide_item(self, item_id: str, decision: str) -> Any:
        return self.store().decide_item(item_id, decision)

    def write_approved_item_to_vault(self, item_id: str) -> Any:
        return self.store().write_approved_item_to_vault(item_id)

    def build_active_review(self) -> PersonalInboxDailyReview:
        return self.store().build_active_review()

    def build_daily_review(self) -> PersonalInboxDailyReview:
        return self.store().build_daily_review()

    def build_combined_review(self) -> PersonalInboxDailyReview:
        return combined_review(self.build_active_review(), self.build_daily_review())

    def handle_slash_command(
        self,
        raw_args: str,
        *,
        source_metadata: dict[str, Any] | None = None,
        capture_prefix: str = "Captured:",
        include_editable_fields: bool = False,
        list_title: str = "Inbox",
    ) -> str:
        parts = shlex.split(raw_args or "")
        if not parts:
            return (
                "Usage: /inbox add <text> | /inbox list | /inbox review | "
                "/inbox approve <id> | /inbox defer <id> | /inbox freeze <id> | "
                "/inbox archive <id> | /inbox trash <id>"
            )

        command = parts[0].lower()
        if command in {"add", "capture"}:
            split_args = raw_args.split(None, 1)
            text = split_args[1].strip() if len(split_args) > 1 else ""
            if not text:
                return "Usage: /inbox add <text>"
            metadata = source_metadata or {
                "source_channel": "slash",
                "surface": "plugin_slash_command",
                "platform": "local",
            }
            response = self.capture({"original_text": text, "source_metadata": metadata})
            lines = [f"{capture_prefix} {format_item(response.item)}"]
            if include_editable_fields:
                lines.append(f"Editable before approval: {', '.join(response.editable_draft_fields)}")
                lines.append("Approval required before any Obsidian vault write.")
            return "\n".join(lines)

        if command == "list":
            items = self.list_items()
            if not items:
                return "Inbox is empty."
            return f"{list_title}:\n" + "\n".join(f"- {format_item(item)}" for item in items)

        if command == "review":
            return self.render_review()

        if command in {"approve", "defer", "freeze", "archive", "trash"}:
            if len(parts) < 2:
                return f"Usage: /inbox {command} <id>"
            item = self.decide_item(parts[1], command)
            message = f"{command}: {format_item(item)}"
            if command == "approve":
                try:
                    path = self.write_approved_item_to_vault(item.item_id)
                    message += f"\nWrote Obsidian note: {path}"
                except Exception as exc:
                    message += f"\nVault write skipped: {exc}"
            return message

        return f"Unknown /inbox command: {command}"

    def render_review(self) -> str:
        store = self.store()
        items = store.list_items()
        active_review = store.build_active_review()
        daily_review = store.build_daily_review()
        lines = ["Daily Inbox Review"]
        if items:
            lines.append("\nPending items:")
            lines.extend(f"- {format_item(item)}" for item in items)
        else:
            lines.append("\nNo pending Inbox items.")
        if active_review.daily_priorities:
            lines.append("\nTop priorities:")
            lines.extend(f"- {priority}" for priority in active_review.daily_priorities)
        if active_review.reflection_questions:
            lines.append("\nReview prompts:")
            lines.extend(f"- {question}" for question in active_review.reflection_questions)
        if daily_review.daily_priorities:
            lines.append("\nApproved daily priorities:")
            lines.extend(f"- {priority}" for priority in daily_review.daily_priorities)
        if daily_review.reflection_questions:
            lines.append("\nGenerated reflection questions:")
            lines.extend(f"- {question}" for question in daily_review.reflection_questions)
        candidate_blog_items = dedupe(active_review.candidate_blog_items + daily_review.candidate_blog_items)
        candidate_report_items = dedupe(active_review.candidate_report_items + daily_review.candidate_report_items)
        if candidate_blog_items:
            lines.append("\nBlog candidates: " + ", ".join(candidate_blog_items))
        if candidate_report_items:
            lines.append("\nReport candidates: " + ", ".join(candidate_report_items))
        return "\n".join(lines)


def load_personal_inbox(
    *,
    store_factory: StoreFactory | None = None,
    capture_func: CaptureFunc | None = None,
) -> PersonalInboxRuntime:
    """Return the one-call embeddable runtime entrypoint."""

    return PersonalInboxRuntime(store_factory=store_factory, capture_func=capture_func)


def register_personal_inbox(ctx: Any, runtime: PersonalInboxRuntime | None = None) -> None:
    """Register Personal Inbox commands with a plugin context."""

    runtime = runtime or load_personal_inbox()
    ctx.register_command(
        "personal-inbox",
        runtime.handle_slash_command,
        description="Capture and review Personal Inbox items.",
        args_hint="[add|list|review|approve|defer|freeze|archive|trash]",
    )
    ctx.register_command(
        "review",
        lambda raw_args: runtime.render_review(),
        description="Show Personal Inbox daily review.",
    )


async def dispatch_gateway_inbox_command(event: Any) -> str:
    """Gateway compatibility adapter for the built-in `/inbox` shim."""

    runtime = load_personal_inbox()
    metadata = gateway_event_source_metadata(
        event,
        source_channel="gateway_chat",
        surface="gateway_slash_command",
    )
    return runtime.handle_slash_command(
        event.get_command_args().strip(),
        source_metadata=metadata,
        capture_prefix="Captured to Personal Inbox:",
        include_editable_fields=True,
        list_title="Personal Inbox",
    )
