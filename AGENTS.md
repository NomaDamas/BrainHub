# BrainHub Development Guide

BrainHub is a small module-runtime project. Keep the repository focused on
reusable Python APIs, host-compatible plugin adapters, and tests for those
interfaces.

## Project Shape

```text
brainhub/
  personal_inbox.py          Core Personal Inbox data model and storage API
plugins/personal_inbox/
  runtime.py                 Embeddable runtime and host command adapter
  __init__.py                Plugin registration surface
tests/
  test_personal_inbox.py     Focused runtime and plugin tests
```

## Engineering Rules

- Core business logic belongs in `brainhub/`.
- Host integration logic belongs in `plugins/personal_inbox/`.
- Keep adapters thin: they should call the runtime rather than duplicate
  capture, review, storage, or vault-write logic.
- Do not add broad agent, gateway, desktop, website, or model-provider code to
  this repository.
- Keep dependencies minimal and pinned in `pyproject.toml`.
- Prefer local state under `BRAINHUB_HOME`; use host-specific environment
  variables only as compatibility fallbacks.

## Validation

```bash
python3 -m pytest -q
python3 -m compileall brainhub plugins/personal_inbox
git diff --check
```
