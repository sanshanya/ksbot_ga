---
name: wps-chat
description: WPS chat, attachments, company-document search, and smart-document read/create/update/share capabilities.
---

# WPS chat and documents capability

Execution surface: `scripts/wps_chat.py` through GA `code_run`. `.wps_context.json` binds the current WPS identity; chat IDs, credentials, and tokens are not command parameters.

```powershell
python "<THIS_SKILL_DIR>/scripts/wps_chat.py" history --limit 30
python "<THIS_SKILL_DIR>/scripts/wps_chat.py" history --participant "甘小雨"
python "<THIS_SKILL_DIR>/scripts/wps_chat.py" history --keyword "baseline"
python "<THIS_SKILL_DIR>/scripts/wps_chat.py" download-latest
python "<THIS_SKILL_DIR>/scripts/wps_chat.py" download --message-id MESSAGE_ID --attachment 1
python "<THIS_SKILL_DIR>/scripts/wps_chat.py" document --url URL
python "<THIS_SKILL_DIR>/scripts/wps_chat.py" document --file-id FILE_ID
python "<THIS_SKILL_DIR>/scripts/wps_chat.py" document-create --title "TITLE" --content-file "MARKDOWN_PATH"
python "<THIS_SKILL_DIR>/scripts/wps_chat.py" document-append --file-id FILE_ID --content-file "MARKDOWN_PATH"
python "<THIS_SKILL_DIR>/scripts/wps_chat.py" document-share --file-id FILE_ID --scope anyone
python "<THIS_SKILL_DIR>/scripts/wps_chat.py" document-search --keyword "KEYWORD" --type content
```

## Operations

- `history`: WPS-visible messages with sender, time, ID, text, and attachment metadata.
- `download`: event-scoped local attachment path; download failures are runtime observations.
- `document`: read-only WPS URL/file-ID lookup returning title, source, content, and errors.
- `document-search`: company-doc locator; `content` = 正文, `file_name` = 标题; `all` may miss smart-doc正文索引.
- `document-create`: Markdown smart-document creation with `scope=anyone`; returns file ID/link when available. External side effect; uncertain results may contain the existing file ID.
- `document-append`: Markdown append to an existing smart document; external content mutation.
- `document-share`: visibility change or repair; scope `anyone` or `company`.

Current-message document metadata: `cloud_docs.txt` contains URLs; `shared_doc_ids.txt` contains IDs. Docs use the authenticated `kdocs-cli` keychain session, separate from WPS Chat credentials. Reply links use `[open](URL)`.

Extended Kdocs capabilities: [kdocs-skill](https://github.com/kdocs-app/kdocs-skill). Availability follows the installed `kdocs-cli` version and authenticated keychain session.
