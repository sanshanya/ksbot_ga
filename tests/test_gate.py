from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest

from ga_core.gate import KubectlAiGate, call_fingerprint, is_kubectl_code


def inventory(tmp_path: Path) -> Path:
    path = tmp_path / "clusters.yaml"
    path.write_text(
        """version: 2
contexts:
  qy-online:
    environment_scope: namespace
    namespaces:
      kaic-kis:
        environment: production
        write_policy: approval_required
      test-inference:
        environment: test
        write_policy: allow
    cluster_scoped_write_policy: approval_required
    all_namespaces_write_policy: approval_required
    unlisted_namespace_write_policy: allow
policy:
  unknown_context: model_fixable
gate_input:
  max_referenced_files: 4
  max_referenced_bytes_per_file: 20000
""",
        encoding="utf-8",
    )
    return path


def _fake_probe(context: str = "qy-online", namespace: str = "kaic-kis"):
    """Stub the kubectl probe so tests don't depend on a real kubeconfig."""
    return lambda cwd: SimpleNamespace(
        current_context=context, current_namespace=namespace, error=""
    )


def _fake_ai_response(decision: str, message: str):
    """Canned urlopen context manager exercising the real _call_ai HTTP path.

    mock=urlopen (AI API network boundary)=>no real AI verification (external paid service)
    """
    body = json.dumps(
        {
            "choices": [
                {"message": {"content": json.dumps({"decision": decision, "message": message})}}
            ]
        }
    ).encode()
    resp = MagicMock()
    resp.read.return_value = body
    ctx = MagicMock()
    ctx.__enter__.return_value = resp
    ctx.__exit__.return_value = None
    return ctx


# TEST-CONTRACT: req=GATE-REFERRAL-01 | rejects=non-kubectl code reaches AI gate (false referral) | gap=no referral filter | revert=remove _KUBECTL_RE regex | mock=none
@pytest.mark.parametrize(
    "code, expected",
    [
        ("kubectl get pods", True),
        (r"C:\\tools\\kubectl.exe get pods", True),
        ("mykubectl get pods", False),
        ("kubectl-wrapper get pods", False),
    ],
)
def test_referral_filter_is_only_standalone_kubectl(code: str, expected: bool) -> None:
    assert is_kubectl_code(code) is expected


# TEST-CONTRACT: req=GATE-REFERRAL-02 | rejects=non-kubectl code is gated | gap=no filter bypass case | revert=remove is_kubectl_code guard in review() | mock=none
def test_non_referred_code_never_calls_gate(tmp_path: Path) -> None:
    gate = KubectlAiGate(inventory_path=inventory(tmp_path))
    result = gate.review("rm -rf /tmp/demo", "bash", str(tmp_path))
    assert result.decision == "allow"
    assert result.source == "filter"


# TEST-CONTRACT: req=GATE-AI-01 | rejects=AI allow verdict is ignored and write is blocked | gap=no authoritative AI path | revert=remove ai_gate return path | mock=urlopen (AI API network boundary)=>no real AI verification (external paid service)
def test_ai_verdict_is_authoritative(tmp_path: Path) -> None:
    gate = KubectlAiGate(
        inventory_path=inventory(tmp_path), base_url="http://gate/v1", model="gate-model"
    )
    gate._probe = _fake_probe()
    with patch("ga_core.gate.urlopen", return_value=_fake_ai_response("allow", "read-only")):
        result = gate.review("kubectl get pods -n kaic-kis", "bash", str(tmp_path))
    assert result.decision == "allow"
    assert result.source == "ai_gate"


# TEST-CONTRACT: req=GATE-FAILCLOSED-02 | rejects=invalid AI JSON is treated as allow | gap=no invalid-output case | revert=remove decision validation in review() | mock=urlopen (AI API network boundary)=>no real AI verification (external paid service)
def test_invalid_ai_output_fails_closed(tmp_path: Path) -> None:
    gate = KubectlAiGate(
        inventory_path=inventory(tmp_path), base_url="http://gate/v1", model="gate-model"
    )
    gate._probe = _fake_probe()
    with patch("ga_core.gate.urlopen", return_value=_fake_ai_response("maybe", "uncertain")):
        result = gate.review("kubectl delete pod x", "bash", str(tmp_path))
    assert result.decision == "approval_required"
    assert result.source == "fail_closed"


# TEST-CONTRACT: req=GATE-FAILCLOSED-03 | rejects=network error allows kubectl to proceed | gap=no network-error case | revert=remove inner AI-call except handler in review() | mock=urlopen (AI API network boundary)=>no real AI verification (external paid service)
def test_gate_network_error_fails_closed(tmp_path: Path) -> None:
    gate = KubectlAiGate(
        inventory_path=inventory(tmp_path), base_url="http://gate/v1", model="gate-model"
    )
    gate._probe = _fake_probe()
    with patch("ga_core.gate.urlopen", side_effect=URLError("connection refused")):
        result = gate.review("kubectl delete pod x -n kaic-kis", "bash", str(tmp_path))
    assert result.decision == "approval_required"
    assert result.source == "fail_closed"


# TEST-CONTRACT: req=GATE-MODELFIX-01 | rejects=model_fixable decision is fail-closed as invalid | gap=no model_fixable pass-through | revert=remove model_fixable from _DECISIONS set | mock=urlopen (AI API network boundary)=>no real AI verification (external paid service)
def test_gate_model_fixable_passes_through_as_valid(tmp_path: Path) -> None:
    gate = KubectlAiGate(
        inventory_path=inventory(tmp_path), base_url="http://gate/v1", model="gate-model"
    )
    gate._probe = _fake_probe()
    with patch(
        "ga_core.gate.urlopen",
        return_value=_fake_ai_response("model_fixable", "namespace not resolved"),
    ):
        result = gate.review("kubectl apply -f deploy.yaml", "bash", str(tmp_path))
    assert result.decision == "model_fixable"
    assert result.source == "ai_gate"
    assert result.message == "namespace not resolved"


# TEST-CONTRACT: req=GATE-FINGERPRINT-01 | rejects=different code/cwd/type shares approval fingerprint | gap=no fingerprint binding | revert=remove any of code/code_type/cwd from hash payload | mock=none
def test_fingerprint_binds_type_cwd_and_exact_code(tmp_path: Path) -> None:
    other_cwd = tmp_path / "other"
    other_cwd.mkdir()
    base = call_fingerprint("code_run", "bash", str(tmp_path), "kubectl get pods")
    # Vary code_type.
    assert base != call_fingerprint(
        "code_run", "powershell", str(tmp_path), "kubectl get pods"
    )
    # Vary cwd.
    assert base != call_fingerprint(
        "code_run", "bash", str(other_cwd), "kubectl get pods"
    )
    # Vary exact code (trailing space).
    assert base != call_fingerprint("code_run", "bash", str(tmp_path), "kubectl get pods ")


# TEST-CONTRACT: req=GATE-MANIFEST-01 | rejects=referenced manifest not attached to AI context | gap=no manifest collection | revert=return empty dict in _collect_referenced_inputs | mock=none
def test_referenced_manifest_is_attached_within_workspace(tmp_path: Path) -> None:
    manifest = tmp_path / "deploy.yaml"
    manifest.write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  namespace: kaic-kis\n",
        encoding="utf-8",
    )
    gate = KubectlAiGate(inventory_path=inventory(tmp_path))
    attached = gate._collect_referenced_inputs(
        "kubectl apply -f deploy.yaml",
        str(tmp_path),
        {"max_referenced_files": 4, "max_referenced_bytes_per_file": 20000},
    )
    assert "namespace: kaic-kis" in attached["manifest:deploy.yaml"]["content"]


# TEST-CONTRACT: req=GATE-MANIFEST-02 | rejects=manifest outside workspace is read (path traversal) | gap=no outside-workspace case | revert=remove relative_to check | mock=none
def test_referenced_manifest_outside_workspace_is_not_read(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.yaml"
    outside.write_text("metadata: {}\n", encoding="utf-8")
    gate = KubectlAiGate(inventory_path=inventory(tmp_path))
    attached = gate._collect_referenced_inputs(
        f'kubectl apply -f "{outside}"',
        str(tmp_path),
        {"max_referenced_files": 4, "max_referenced_bytes_per_file": 20000},
    )
    assert attached[f"manifest:{outside}"]["error"] == "path is outside the workspace"


# TEST-CONTRACT: req=GATE-SCRIPT-01 | rejects=explicit local script containing kubectl bypasses referral | gap=no script-content inspection | revert=remove _SCRIPT_PATTERNS collection | mock=urlopen boundary
def test_explicit_script_with_kubectl_is_reviewed(tmp_path: Path) -> None:
    script = tmp_path / "deploy.py"
    script.write_text("import os\nos.system('kubectl delete pod x -n kaic-kis')\n", encoding="utf-8")
    gate = KubectlAiGate(
        inventory_path=inventory(tmp_path), base_url="http://gate/v1", model="gate-model"
    )
    gate._probe = _fake_probe()
    with patch(
        "ga_core.gate.urlopen",
        return_value=_fake_ai_response("approval_required", "会删除生产 Pod"),
    ):
        result = gate.review("python deploy.py", "bash", str(tmp_path))
    assert result.decision == "approval_required"
    assert result.source == "ai_gate"


# TEST-CONTRACT: req=GATE-SCRIPT-02 | rejects=dynamic script path silently bypasses Gate | gap=no unresolved-script path | revert=allow script input errors | mock=none
def test_dynamic_script_path_is_model_fixable(tmp_path: Path) -> None:
    gate = KubectlAiGate(inventory_path=inventory(tmp_path))
    result = gate.review('bash "$DEPLOY_SCRIPT"', "bash", str(tmp_path))
    assert result.decision == "model_fixable"


# TEST-CONTRACT: req=CLUSTERS-01 | rejects=production namespace treated as protected and test namespace as approval_required | gap=no inventory validation | revert=remove write_policy from clusters.yaml | mock=none
def test_project_inventory_distinguishes_production_from_protection() -> None:
    import yaml

    root = Path(__file__).parents[1]
    data = yaml.safe_load((root / "config" / "clusters.yaml").read_text(encoding="utf-8"))
    namespaces = data["contexts"]["qy-online"]["namespaces"]
    assert namespaces["kaic-kis"]["environment"] == "production"
    assert namespaces["kaic-kis"]["write_policy"] == "approval_required"
    assert namespaces["test-inference"]["write_policy"] == "allow"
    assert namespaces["kube-system"]["environment"] == "system"
    assert namespaces["kube-system"]["write_policy"] == "approval_required"
    assert namespaces["default"]["environment"] == "default"
    assert namespaces["default"]["write_policy"] == "approval_required"
    assert data["contexts"]["qy-online"]["unlisted_namespace_write_policy"] == "allow"
