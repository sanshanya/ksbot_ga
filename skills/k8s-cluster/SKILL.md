---
name: k8s-cluster
description: Activate for any Qingyang Kubernetes request, including inspection, logs, capacity, health, or kubectl.
version: "4"
---

# Kubernetes operating SOP

Read `config/clusters.yaml` first; it is the sole source for context, namespace identity, and write policy. Do not infer environment from node/model labels or the word `online`.

Use kubeconfig `C:/Users/sansm/.kube/qy-online.yaml` and context `qy-online` (or a declared alias). Environment ownership is namespace/Pod-level; nodes are shared and must not be classified as test or production.

| Namespace/scope | Meaning | Write policy |
|---|---|---|
| `kaic-kis` | production inference | approval |
| `test-inference` | experiments | autonomous |
| `kube-system`, `default` | protected, not production | approval |
| all namespaces / cluster scope | shared impact | approval |

Before each command, make context and namespace explicit when practical. Read-only queries are allowed. After every write, verify intended state with an independent read (`get`, `rollout status`, events, or equivalent); exit code alone is not success. A rejected/timed-out write must not be rephrased to bypass Gate. For `model_fixable`, expose the missing context, namespace, kubeconfig, manifest, or script path before retrying. Never hide or construct the `kubectl` token to evade referral.

For manifests: read the complete file; inspect every namespace (including `kind: List` items); use `kubectl diff` when supported; keep context/kubeconfig explicit for writes; verify rollout/status/events afterward.

Operational queries:
- “test machines/instances” means Pods in `test-inference`; cluster nodes means `kubectl get nodes`.
- Use live output; never reconstruct truncated node lists.
- Prefer `kubectl top nodes --no-headers`; describe individual nodes only when detail is needed.
- Select GPU models with real labels such as `accelerator` or `nvidia.com/gpu.product`.

For free GPU capacity, use two aggregate reads: nodes JSON for capacity/allocatable and all-Pods JSON for GPU requests. Join locally by `spec.nodeName`, count unused nodes as `used=0`, and report total capacity, requested, free, and anomalies. Do not query per node.
