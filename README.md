# KSBot GA

A thin WPS product layer around upstream GenericAgent. It deliberately avoids a fork and
does not copy the GA loop.

**Project design and implementation: [`docs/PROJECT.md`](docs/PROJECT.md)**

## Setup

```powershell
# Windows / PowerShell
uv sync --extra dev --extra ui
uv run python scripts/fetch_ga.py

# GitHub direct access is unstable in some networks. Use the local proxy when needed:
uv run python scripts/fetch_ga.py --proxy http://127.0.0.1:10090

Copy-Item .env.example .env
notepad .env                       # configure WPS; Gate reads mykey.py by default
# In WPS developer settings, grant kso.contact.read so group reply @ labels use real user names.
notepad config\clusters.yaml     # verify qy-online namespace identities and write policies
# Generate GA local config (mykey.py + optional vision_api.py):
uv run python scripts\configure_ga_local.py --local
# Existing config is preserved; use --force only when replacement is intentional.
notepad vendor\GenericAgent\mykey.py
Push-Location bridge
npm install
Pop-Location

# After apikey/apibase/model are configured in mykey.py:
uv run ga-wps

# Optional local interfaces using upstream GA UI code
uv run ga-wps-tui
uv run ga-wps-streamlit
```

```bash
# Linux / macOS
uv sync --extra dev --extra ui
uv run python scripts/fetch_ga.py
cp .env.example .env
(cd bridge && npm install)
uv run ga-wps
```

`uv sync` creates `.venv` but does not activate it in the current shell. Prefer `uv run ...`
for all project commands. The fetcher performs a full (non-shallow) blobless Git clone
(`--filter=blob:none --no-checkout`) and checks out `GA_REVISION` as a detached HEAD, so
`git log`/`git diff`/`git blame` work across future upgrades.

## Troubleshooting: no model configuration

If startup reports that no LLM session was loaded, verify that
`vendor/GenericAgent/mykey.py` exists and contains at least one active
`native_oai_config` or `native_claude_config`. The quickest way to create it is:

```powershell
uv run python scripts\configure_ga_local.py --local
```

This copies `examples/mykey.local.example.py` (apikey `EMPTY`, local GLM-5.2 endpoint) into
`vendor/GenericAgent/mykey.py`. Edit the file to match your real endpoint. Merely copying the
upstream `mykey_template.py` is not enough because its model examples are commented out.

For vision (image input) support, install the optional dependency and generate the config:

```powershell
uv sync --extra vision
uv run python scripts\configure_ga_local.py --vision-only
```

Then edit `vendor/GenericAgent/memory/vision_api.py` to set `OPENAI_CONFIG_KEY` and
`VISION_MODEL`. The model must support image input.

## License

KSBot GA is released under the MIT License. GenericAgent is fetched as an independent
MIT-licensed upstream checkout pinned by `GA_REVISION`; its own copyright and license
remain in `vendor/GenericAgent`.
