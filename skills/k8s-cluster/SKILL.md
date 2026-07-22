---
name: k8s-cluster
description: Qingyang Kubernetes inspection, logs, capacity, health, and kubectl write capabilities.
version: "4"
---

# Qingyang Kubernetes capabilities

`config/clusters.yaml` is authoritative for cluster identity, namespaces, and write policy. Default connection: kubeconfig `C:/Users/sansm/.kube/qy-online.yaml`, context `qy-online` (or a declared alias). Environment ownership is namespace/Pod scoped; nodes are shared.

| Namespace/scope | Meaning | Write policy |
|---|---|---|
| `kaic-kis` | production inference | approval |
| `test-inference` | experiments | autonomous |
| `kube-system`, `default` | protected system scope | approval |
| all namespaces / cluster scope | shared impact | approval |

## Execution contract

- Read-only queries have no write side effect.
- Protected writes carry Gate status and approval context; rejection or timeout means `not executed`.
- A successful write has an independent state observation; exit code alone is not a verified postcondition.
- `model_fixable` identifies missing namespace, kubeconfig, manifest, or script context.
- Write command context is explicit where supported; Kubernetes credentials are not command text.
- Manifest state includes the complete file and every namespace, including `kind: List` items. Diff, rollout, events, and resulting object state are available observations.

## Query semantics

- “test machines/instances” = Pods in `test-inference`; “cluster nodes” = `kubectl get nodes`.
- GPU identity comes from live labels such as `accelerator` or `nvidia.com/gpu.product`.
- Free GPU capacity joins aggregate node capacity/allocatable with aggregate Pod GPU requests by `spec.nodeName`; the result contains total, requested, free, and anomalies.
