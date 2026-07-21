---
name: wps-chat
description: Read/search the current WPS chat or retrieve earlier attachments through the bundled script.
---

# WPS chat

Run `scripts/wps_chat.py` by absolute path with GA `code_run`. The chat workspace `.wps_context.json` supplies identity; never invent or pass chat IDs, credentials, or tokens.

```powershell
python "<THIS_SKILL_DIR>/scripts/wps_chat.py" history --limit 30
python "<THIS_SKILL_DIR>/scripts/wps_chat.py" history --participant "甘小雨"
python "<THIS_SKILL_DIR>/scripts/wps_chat.py" history --keyword "baseline"
python "<THIS_SKILL_DIR>/scripts/wps_chat.py" download-latest
python "<THIS_SKILL_DIR>/scripts/wps_chat.py" download --message-id MESSAGE_ID --attachment 1
```

`history` returns the latest matches across all WPS-visible history with source, time, IDs, senders, and attachment metadata; rerun when freshness matters. Downloads go under the current workspace `downloads/`; read them with `file_read`/`code_run`, and place user deliverables under `artifacts/`.
