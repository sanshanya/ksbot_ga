from __future__ import annotations

import dataclasses
import importlib.util
import os
from dataclasses import dataclass
from pathlib import Path


def _resolve_gate_from_mykey(ga_root: Path, config_key: str) -> dict[str, str]:
    """Read one public model config dictionary from GenericAgent's mykey.py."""
    mykey_path = ga_root / "mykey.py"
    if not mykey_path.is_file():
        return {}
    spec = importlib.util.spec_from_file_location("_gate_mykey", mykey_path)
    if not spec or not spec.loader:
        return {}
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return {}
    config = getattr(module, config_key, None)
    if not isinstance(config, dict):
        return {}
    return {
        "apibase": str(config.get("apibase", "")),
        "apikey": str(config.get("apikey", "")),
        "model": str(config.get("model", "")),
    }


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        if key.strip():
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class CoreSettings:
    project_root: Path
    ga_root: Path
    runtime_root: Path
    cluster_config: Path
    max_turns: int
    gate_base_url: str
    gate_api_key: str
    gate_model: str
    gate_config_key: str
    gate_timeout_seconds: int
    kubectl_probe_timeout_seconds: int

    @classmethod
    def from_env(cls, project_root: Path | None = None) -> "CoreSettings":
        root = (project_root or Path.cwd()).resolve()
        load_dotenv(root / ".env")
        return cls(
            project_root=root,
            ga_root=Path(os.getenv("GA_ROOT", root / "vendor" / "GenericAgent")).resolve(),
            runtime_root=Path(os.getenv("GA_RUNTIME_ROOT", root / "runtime")).resolve(),
            cluster_config=Path(
                os.getenv("GA_CLUSTER_CONFIG", root / "config" / "clusters.yaml")
            ).resolve(),
            max_turns=env_int("GA_MAX_TURNS", 80),
            gate_base_url=os.getenv("GA_GATE_BASE_URL", ""),
            gate_api_key=os.getenv("GA_GATE_API_KEY", ""),
            gate_model=os.getenv("GA_GATE_MODEL", ""),
            gate_config_key=os.getenv("GA_GATE_CONFIG_KEY", "native_oai_config"),
            gate_timeout_seconds=env_int("GA_GATE_TIMEOUT_SECONDS", 30),
            kubectl_probe_timeout_seconds=env_int("GA_KUBECTL_PROBE_TIMEOUT_SECONDS", 5),
        )

    def resolve_gate_config(self) -> "CoreSettings":
        if self.gate_base_url and self.gate_model:
            return self
        config = _resolve_gate_from_mykey(self.ga_root, self.gate_config_key)
        return dataclasses.replace(
            self,
            gate_base_url=self.gate_base_url or config.get("apibase", ""),
            gate_api_key=self.gate_api_key or config.get("apikey", ""),
            gate_model=self.gate_model or config.get("model", ""),
        )

    def validate(self) -> None:
        if not self.ga_root.joinpath("agent_loop.py").is_file():
            raise RuntimeError(f"GenericAgent not found at {self.ga_root}")
        if not self.cluster_config.is_file():
            raise RuntimeError(f"Cluster inventory not found: {self.cluster_config}")
        self.runtime_root.mkdir(parents=True, exist_ok=True)
