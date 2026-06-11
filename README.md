# BrainHub

BrainHub is an embeddable Python module runtime for personal workflow capture.
It exposes reusable APIs first, then optional host adapters for agent/plugin
environments.

## Current Module

Personal Inbox captures raw thoughts, tasks, schedule notes, project notes,
blog ideas, report candidates, and memory fragments into a separate review
queue. Items stay in a local JSON store until they are reviewed. Obsidian vault
writes are blocked until an item is explicitly approved.

## Boundary

| Layer | Responsibility |
| --- | --- |
| `brainhub/` | Core data model, storage, review, and capture APIs |
| `plugins/personal_inbox/` | Thin host-compatible plugin/runtime adapter |
| `tests/` | Focused runtime and adapter coverage |

Feature logic belongs in `brainhub/`. Host adapters should call the runtime and
avoid copying capture, review, storage, or vault-write behavior.

## Runtime API

```python
from plugins.personal_inbox.runtime import load_personal_inbox

runtime = load_personal_inbox()
response = runtime.capture({
    "original_text": "Blog idea: explain Inbox review safety.",
    "source_metadata": {
        "source_channel": "api",
        "surface": "embedded_runtime",
        "platform": "local",
    },
})

items = runtime.list_items()
review = runtime.build_combined_review()
```

For direct core access:

```python
from brainhub.personal_inbox import PersonalInboxStore, capture_personal_inbox_item

response = capture_personal_inbox_item({
    "original_text": "Task: publish the standalone BrainHub module.",
    "source_channel": "api",
})

store = PersonalInboxStore()
pending = store.list_items()
```

## Host Adapter

The Personal Inbox adapter registers:

| Surface | Names |
| --- | --- |
| Tools | `personal_inbox_add`, `personal_inbox_list`, `personal_inbox_read`, `personal_inbox_update`, `personal_inbox_decide`, `personal_inbox_review` |
| Slash commands | `/personal-inbox ...`, `/review` |
| Compatibility command | `/inbox ...` through `dispatch_gateway_inbox_command(event)` |

Supported inbox commands:

```text
/inbox add <text>
/inbox capture <text>
/inbox list
/inbox review
/inbox approve <id>
/inbox defer <id>
/inbox freeze <id>
/inbox archive <id>
/inbox trash <id>
```

## Data Safety

Personal Inbox is intentionally separate from session state, memory stores, and
Obsidian vault files.

Default storage:

```text
~/.brainhub/personal_inbox/items.json
```

Configuration:

```text
BRAINHUB_HOME=~/.brainhub
BRAINHUB_OBSIDIAN_VAULT_PATH=~/Documents/ObsidianVault
```

Approved items may be written to an Obsidian vault only after review approval.

## Development

```bash
python3 -m pytest -q
python3 -m compileall brainhub plugins/personal_inbox
git diff --check
```

## Project Policy

This repository is maintained as a private/personal project surface. It does
not document public onboarding or third-party maintainer workflows.

Changes should preserve the module-runtime boundary and keep host-specific
integration logic outside the BrainHub core package.

## License

See [LICENSE](LICENSE).
