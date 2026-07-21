# KSBot GA

WPS 365 GenericAgent：保留上游 GA loop、工具和全局记忆，仅增加 WPS transport 与 Kubernetes AI Gate。设计见 [`docs/PROJECT.md`](docs/PROJECT.md)。

```powershell
uv sync --extra dev
uv run python scripts/fetch_ga.py
Copy-Item .env.example .env
uv run python scripts/configure_ga_local.py
Push-Location bridge; npm install; Pop-Location
uv run ga-wps
```

`uv run` 无需激活 `.venv`。模型配置位于 `vendor/GenericAgent/mykey.py`，集群事实位于 `config/clusters.yaml`。

MIT；固定 revision 的 GenericAgent 保留其上游许可。
