"""Personal Inbox plugin.

The plugin deliberately keeps capture separate from Obsidian writes. Users can
capture freely, review item-by-item, and only approved items are written to a
configured Obsidian vault.
"""

from __future__ import annotations

import json
from typing import Any

from brainhub.personal_inbox import (
    PERSONAL_INBOX_CAPTURE_REQUEST_SCHEMA,
    PersonalInboxStore,
    capture_personal_inbox_item,
)
from plugins.personal_inbox.runtime import (
    PersonalInboxRuntime,
    combined_review,
    dispatch_gateway_inbox_command,
    format_item,
    load_personal_inbox,
    register_personal_inbox,
)

__all__ = [
    "PersonalInboxRuntime",
    "dispatch_gateway_inbox_command",
    "load_personal_inbox",
    "register_personal_inbox",
    "register",
]


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _item_payload(item) -> dict[str, Any]:
    return item.to_dict()


def _store() -> PersonalInboxStore:
    return PersonalInboxStore()


def _runtime() -> PersonalInboxRuntime:
    return load_personal_inbox(capture_func=capture_personal_inbox_item)


def _handle_add(args: dict[str, Any], **_: Any) -> str:
    request_data = dict(args)
    source_metadata = request_data.get("source_metadata")
    if "source_channel" not in request_data and not (
        isinstance(source_metadata, dict) and source_metadata.get("source_channel")
    ):
        request_data["source_channel"] = "agent"
    response = capture_personal_inbox_item(request_data)
    return _json(response.to_dict())


def _handle_read(args: dict[str, Any], **_: Any) -> str:
    item_id = str(args.get("item_id") or "").strip()
    item = _store().read_item(item_id)
    return _json({"ok": True, "item": _item_payload(item)})


def _handle_list(args: dict[str, Any], **_: Any) -> str:
    raw_statuses = args.get("statuses") or ["pending", "edited", "deferred"]
    statuses = tuple(str(status).strip() for status in raw_statuses if str(status).strip())
    items = [_item_payload(item) for item in _store().list_items(statuses=statuses or None)]
    return _json({"ok": True, "items": items})


def _handle_update(args: dict[str, Any], **_: Any) -> str:
    item_id = str(args.get("item_id") or "").strip()
    edits = dict(args.get("edits") or {})
    item = _store().update_item(item_id, **edits)
    return _json({"ok": True, "item": _item_payload(item)})


def _handle_decide(args: dict[str, Any], **_: Any) -> str:
    item_id = str(args.get("item_id") or "").strip()
    decision = str(args.get("decision") or "").strip()
    store = _store()
    item = store.decide_item(item_id, decision)
    payload: dict[str, Any] = {"ok": True, "item": _item_payload(item)}
    if item.approval_status == "approved" and args.get("write_vault", True):
        try:
            payload["vault_path"] = str(store.write_approved_item_to_vault(item_id))
        except Exception as exc:
            payload["vault_error"] = str(exc)
    return _json(payload)


def _handle_review(args: dict[str, Any], **_: Any) -> str:
    store = _store()
    items = [_item_payload(item) for item in store.list_items()]
    active_review = store.build_active_review()
    daily_review = store.build_daily_review()
    return _json({
        "ok": True,
        "items": items,
        "review": combined_review(active_review, daily_review).to_dict(),
        "active_review": active_review.to_dict(),
        "daily_review": daily_review.to_dict(),
    })


def _format_item(item: dict[str, Any]) -> str:
    return format_item(item)


def _slash_inbox(raw_args: str) -> str:
    return _runtime().handle_slash_command(raw_args)


def _slash_review(_: str) -> str:
    return _runtime().render_review()


_TEXT_SCHEMA = dict(PERSONAL_INBOX_CAPTURE_REQUEST_SCHEMA)


def register(ctx) -> None:
    ctx.register_tool(
        name="personal_inbox_add",
        toolset="personal_inbox",
        schema={
            "name": "personal_inbox_add",
            "description": "Capture raw text into the separate Personal Inbox.",
            "parameters": _TEXT_SCHEMA,
        },
        handler=_handle_add,
        emoji="📥",
    )
    ctx.register_tool(
        name="personal_inbox_list",
        toolset="personal_inbox",
        schema={
            "name": "personal_inbox_list",
            "description": "List Personal Inbox items by review status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "statuses": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Statuses to include.",
                    }
                },
            },
        },
        handler=_handle_list,
        emoji="📋",
    )
    ctx.register_tool(
        name="personal_inbox_read",
        toolset="personal_inbox",
        schema={
            "name": "personal_inbox_read",
            "description": "Read one captured Personal Inbox item by stable ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                },
                "required": ["item_id"],
            },
        },
        handler=_handle_read,
        emoji="🔎",
    )
    ctx.register_tool(
        name="personal_inbox_update",
        toolset="personal_inbox",
        schema={
            "name": "personal_inbox_update",
            "description": "Edit visible draft metadata before approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "edits": {"type": "object"},
                },
                "required": ["item_id", "edits"],
            },
        },
        handler=_handle_update,
        emoji="✏️",
    )
    ctx.register_tool(
        name="personal_inbox_decide",
        toolset="personal_inbox",
        schema={
            "name": "personal_inbox_decide",
            "description": "Approve, defer, archive, freeze, or trash one Inbox item.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "decision": {"type": "string", "enum": ["approve", "defer", "archive", "freeze", "trash"]},
                    "write_vault": {"type": "boolean", "default": True},
                },
                "required": ["item_id", "decision"],
            },
        },
        handler=_handle_decide,
        emoji="✅",
    )
    ctx.register_tool(
        name="personal_inbox_review",
        toolset="personal_inbox",
        schema={
            "name": "personal_inbox_review",
            "description": "Generate a daily review summary from active Inbox items.",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=_handle_review,
        emoji="🧭",
    )
    register_personal_inbox(ctx, _runtime())
