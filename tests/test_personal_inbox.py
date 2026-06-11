import json
import asyncio

import brainhub.personal_inbox as personal_inbox
from brainhub.personal_inbox import (
    EDITABLE_DRAFT_FIELDS,
    PersonalInboxStore,
    build_daily_review,
    capture_personal_inbox_item,
    default_inbox_path,
    inbox_store_path_is_isolated,
)
from plugins.personal_inbox import _handle_add, register
from plugins.personal_inbox.runtime import dispatch_gateway_inbox_command, load_personal_inbox


def test_default_inbox_path_uses_brainhub_home(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAINHUB_HOME", str(tmp_path))

    assert default_inbox_path() == tmp_path / "personal_inbox" / "items.json"
    assert inbox_store_path_is_isolated(default_inbox_path())


def test_runtime_capture_query_review_and_decide(tmp_path):
    inbox_path = tmp_path / "items.json"
    runtime = load_personal_inbox(
        store_factory=lambda: PersonalInboxStore(inbox_path),
        capture_func=lambda request, **kwargs: capture_personal_inbox_item(
            request,
            inbox_path=inbox_path,
            **kwargs,
        ),
    )

    response = runtime.capture({
        "original_text": "Blog idea: document BrainHub runtime boundaries.",
        "source_metadata": {
            "source_channel": "api",
            "surface": "embedded_runtime",
            "platform": "local",
        },
        "priority": "high",
        "next_action": "Write the boundary note",
    })
    item = runtime.read_item(response.item.item_id)
    approved = runtime.decide_item(item.item_id, "approve")
    review = runtime.build_combined_review()

    assert response.ok is True
    assert item.source_metadata["surface"] == "embedded_runtime"
    assert item.blog_candidate is True
    assert approved.approval_status == "approved"
    assert "Write the boundary note" in "\n".join(review.daily_priorities)


def test_plugin_tool_and_command_registration(tmp_path, monkeypatch):
    inbox_path = tmp_path / "items.json"
    monkeypatch.setattr(personal_inbox, "default_inbox_path", lambda: inbox_path)
    registered_tools = {}
    registered_commands = {}

    class FakeContext:
        def register_tool(self, **kwargs):
            registered_tools[kwargs["name"]] = kwargs

        def register_command(self, name, handler, description="", args_hint=""):
            registered_commands[name] = {
                "handler": handler,
                "description": description,
                "args_hint": args_hint,
            }

    register(FakeContext())
    payload = json.loads(_handle_add({"text": "Capture a report candidate."}))

    assert "personal_inbox_add" in registered_tools
    assert "personal-inbox" in registered_commands
    assert "review" in registered_commands
    assert payload["ok"] is True
    assert set(EDITABLE_DRAFT_FIELDS) <= set(payload["editable_draft_fields"])


def test_gateway_adapter_captures_source_metadata(tmp_path, monkeypatch):
    inbox_path = tmp_path / "items.json"
    monkeypatch.setattr(personal_inbox, "default_inbox_path", lambda: inbox_path)

    class Platform:
        value = "telegram"

    class MessageType:
        value = "text"

    class Source:
        platform = Platform()
        chat_id = "chat-1"
        thread_id = "thread-1"
        user_id = "user-1"
        message_id = "source-message"
        chat_name = "BrainHub"
        chat_type = "dm"
        user_name = "owner"

    class Event:
        source = Source()
        message_id = "message-1"
        timestamp = None
        message_type = MessageType()

        def get_command_args(self):
            return "add Report idea: summarize inbox review evidence"

    result = asyncio.run(dispatch_gateway_inbox_command(Event()))
    items = PersonalInboxStore(inbox_path).list_items()

    assert "Captured to Personal Inbox" in result
    assert "Approval required before any Obsidian vault write" in result
    assert len(items) == 1
    assert items[0].source_channel == "gateway_chat"
    assert items[0].source_metadata["surface"] == "gateway_slash_command"
    assert items[0].source_metadata["platform"] == "telegram"
    assert items[0].source_metadata["conversation_id"] == "chat-1"


def test_daily_review_requires_approved_items(tmp_path):
    inbox_path = tmp_path / "items.json"
    store = PersonalInboxStore(inbox_path)
    pending = store.add_item("Pending blog idea", source_channel="api")
    approved = store.add_item("Ship BrainHub module runtime", source_channel="api")
    store.update_item(approved.item_id, priority="high", next_action="Publish standalone repo")
    store.decide_item(approved.item_id, "approve")

    review = build_daily_review(inbox_path=inbox_path)

    assert pending.item_id not in "\n".join(review.daily_priorities)
    assert any("Publish standalone repo" in line for line in review.daily_priorities)
