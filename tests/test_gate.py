from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest
import yaml

from ga_core.gate import KubectlAiGate, is_kubectl_code


def inventory(tmp_path: Path) -> Path:
    path = tmp_path / "clusters.yaml"
    path.write_text(
        """version: 2
contexts:
  qy-online:
    environment_scope: namespace
    namespaces:
      kaic-kis: {environment: production, write_policy: approval_required}
      test-inference: {environment: test, write_policy: allow}
    cluster_scoped_write_policy: approval_required
    all_namespaces_write_policy: approval_required
    unlisted_namespace_write_policy: allow
policy: {unknown_context: model_fixable}
gate_input: {max_referenced_files: 4, max_referenced_bytes_per_file: 20000}
""",
        encoding="utf-8",
    )
    return path


def gate(tmp_path: Path) -> KubectlAiGate:
    value = KubectlAiGate(
        inventory_path=inventory(tmp_path), base_url="http://gate/v1", model="gate-model"
    )
    value._probe = lambda _cwd: SimpleNamespace(
        current_context="qy-online", current_namespace="kaic-kis", error=""
    )
    return value


def response(decision: str, message: str):
    body = json.dumps(
        {"choices": [{"message": {"content": json.dumps({"decision": decision, "message": message})}}]}
    ).encode()
    stream = MagicMock()
    stream.read.return_value = body
    context = MagicMock()
    context.__enter__.return_value = stream
    return context


@pytest.mark.parametrize(
    "code, expected",
    [
        ("kubectl get pods", True),
        (r"C:\tools\kubectl.exe get pods", True),
        ("mykubectl get pods", False),
        ("kubectl-wrapper get pods", False),
    ],
)
def test_referral_requires_standalone_kubectl(code: str, expected: bool) -> None:
    assert is_kubectl_code(code) is expected


def test_non_referred_code_bypasses_gate(tmp_path: Path) -> None:
    result = KubectlAiGate(inventory_path=inventory(tmp_path)).review(
        "rm -rf /tmp/demo", "bash", str(tmp_path)
    )
    assert (result.decision, result.source) == ("allow", "filter")


@pytest.mark.parametrize(
    "ai_decision, expected_decision, expected_source",
    [
        ("allow", "allow", "ai_gate"),
        ("model_fixable", "model_fixable", "ai_gate"),
        ("maybe", "approval_required", "fail_closed"),
    ],
)
def test_ai_output_controls_or_fails_closed(
    tmp_path: Path, ai_decision: str, expected_decision: str, expected_source: str
) -> None:
    with patch("ga_core.gate.urlopen", return_value=response(ai_decision, "review")):
        result = gate(tmp_path).review("kubectl delete pod x -n kaic-kis", "bash", str(tmp_path))
    assert (result.decision, result.source) == (expected_decision, expected_source)


def test_gate_network_failure_requires_approval(tmp_path: Path) -> None:
    with patch("ga_core.gate.urlopen", side_effect=URLError("connection refused")):
        result = gate(tmp_path).review("kubectl delete pod x -n kaic-kis", "bash", str(tmp_path))
    assert (result.decision, result.source) == ("approval_required", "fail_closed")


def test_manifest_inputs_stay_inside_workspace(tmp_path: Path) -> None:
    local = tmp_path / "deploy.yaml"
    local.write_text("metadata:\n  namespace: kaic-kis\n", encoding="utf-8")
    outside = tmp_path.parent / "outside.yaml"
    outside.write_text("metadata: {}\n", encoding="utf-8")
    reviewer = KubectlAiGate(inventory_path=inventory(tmp_path))
    limits = {"max_referenced_files": 4, "max_referenced_bytes_per_file": 20000}
    attached = reviewer._collect_referenced_inputs("kubectl apply -f deploy.yaml", str(tmp_path), limits)
    rejected = reviewer._collect_referenced_inputs(
        f'kubectl apply -f "{outside}"', str(tmp_path), limits
    )
    assert "namespace: kaic-kis" in attached["manifest:deploy.yaml"]["content"]
    assert rejected[f"manifest:{outside}"]["error"] == "path is outside the workspace"


def test_explicit_scripts_are_reviewed_and_dynamic_paths_are_fixable(tmp_path: Path) -> None:
    script = tmp_path / "deploy.py"
    script.write_text("import os\nos.system('kubectl delete pod x -n kaic-kis')\n", encoding="utf-8")
    with patch(
        "ga_core.gate.urlopen",
        return_value=response("approval_required", "会删除生产 Pod"),
    ):
        result = gate(tmp_path).review("python deploy.py", "bash", str(tmp_path))
    assert (result.decision, result.source) == ("approval_required", "ai_gate")
    assert KubectlAiGate(inventory_path=inventory(tmp_path)).review(
        'bash "$DEPLOY_SCRIPT"', "bash", str(tmp_path)
    ).decision == "model_fixable"


def test_project_inventory_keeps_environment_and_policy_separate() -> None:
    data = yaml.safe_load(
        (Path(__file__).parents[1] / "config" / "clusters.yaml").read_text(encoding="utf-8")
    )
    namespaces = data["contexts"]["qy-online"]["namespaces"]
    assert namespaces["kaic-kis"]["environment"] == "production"
    assert namespaces["kaic-kis"]["write_policy"] == "approval_required"
    assert namespaces["test-inference"]["write_policy"] == "allow"
    assert namespaces["kube-system"]["write_policy"] == "approval_required"
    assert data["contexts"]["qy-online"]["unlisted_namespace_write_policy"] == "allow"
