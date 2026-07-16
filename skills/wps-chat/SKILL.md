---
name: wps-chat
description: Read or search the current WPS chat and retrieve attachments from earlier messages by running the bundled script with GA's existing code_run tool.
---

# WPS chat

This Skill adds no model tool. Use GA's existing `code_run` tool to run the sibling script
`scripts/wps_chat.py`. Run it by absolute path derived from this `SKILL.md` path; `code_run` already
starts in the current chat workspace, where `.wps_context.json` binds the script to this chat.
Never invent or pass a `chat_id`, WPS credential, or API token.

## Read current chat history

```powershell
python "<THIS_SKILL_DIR>/scripts/wps_chat.py" history --limit 30
```

Optional filters:

```powershell
python "<THIS_SKILL_DIR>/scripts/wps_chat.py" history --participant "甘小雨"
python "<THIS_SKILL_DIR>/scripts/wps_chat.py" history --keyword "baseline"
```

Use this when the user asks what was said recently, asks about a participant, or when the bootstrap
snapshot may be stale. `--limit N` means the latest N matches across all WPS-visible history. The result identifies its source, fetch time, scope, message IDs, sender
names when available, and attachments. Run it again whenever freshness matters.

## Retrieve an earlier attachment

For “刚才/上一条附件”, scan WPS-visible history and download the newest attachment:

```powershell
python "<THIS_SKILL_DIR>/scripts/wps_chat.py" download-latest
```

For a specific result returned by `history`:

```powershell
python "<THIS_SKILL_DIR>/scripts/wps_chat.py" download --message-id MESSAGE_ID --attachment 1
```

The script writes below the current workspace `downloads/` directory. Read the downloaded file with
`file_read` or `code_run`, then create user deliverables below `artifacts/`.

`session_memory.md` stores selected durable facts and preferences. It is not the WPS transcript and
must not be used as a substitute for live chat history.
