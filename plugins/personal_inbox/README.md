# Personal Inbox Plugin Runtime

Personal Inbox is embeddable as a plugin/runtime module. The stable entrypoint
is `plugins.personal_inbox.runtime.load_personal_inbox()`.

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

## Plugin Registration

The plugin's `register(ctx)` function registers:

- Tools: `personal_inbox_add`, `personal_inbox_list`,
  `personal_inbox_read`, `personal_inbox_update`,
  `personal_inbox_decide`, and `personal_inbox_review`.
- Slash commands: `/personal-inbox ...` and `/review`.

The `/inbox` command can be exposed by a host as a compatibility shim. It
delegates to `dispatch_gateway_inbox_command(event)` in this runtime module, so
gateway behavior stays compatible without duplicating Personal Inbox business
logic in host code.

## Command Surface

The runtime preserves:

- `/inbox add <text>` and `/inbox capture <text>`
- `/inbox list`
- `/inbox review`
- `/inbox approve <id>`
- `/inbox defer <id>`
- `/inbox freeze <id>`
- `/inbox archive <id>`
- `/inbox trash <id>`

Approval is required before any Obsidian vault write. The default JSON store is
profile-scoped at `~/.brainhub/personal_inbox/items.json` unless
`BRAINHUB_HOME` is set by the embedding host.
