---
name: k8s-cluster
description: Activate for ANY request about the Qingyang Kubernetes cluster, including connecting, inspecting, health checks, pods, logs, GPU capacity, or any kubectl command.
version: "3"
---

# k8s-cluster

Before any Kubernetes task, read `config/clusters.yaml`. It is the machine-readable source of
truth for context, namespace identity, and write-approval policy. This Skill is the operating SOP.
Do not infer environment from node names, labels, model names, or the word `online`.

One shared cluster, kubeconfig:

```text
C:/Users/sansm/.kube/qy-online.yaml
```

Use context `qy-online` or an alias explicitly declared in `config/clusters.yaml`.

| Namespace | Purpose | Environment | Write policy |
|---|---|---|---|
| `kaic-kis` | Production inference | Production | Approval required |
| `test-inference` | SGLang experiments | Test | Allowed without Gate approval |
| `kube-system` | Cluster system components | System, not production | Approval required |
| `default` | Default namespace | Protected default, not production | Approval required |

Only `kaic-kis` is the production business environment. `kube-system` and `default` are protected
because accidental writes are dangerous, but they must not be described as production.

## Environment and nodes

Environment ownership is at the Pod/namespace layer, not the node layer. The cluster's nodes are
shared. A node may host both `test-inference` and `kaic-kis` Pods; this is normal.

- “How many test machines/instances?” means query Pods in `test-inference`, not nodes.
- “How many cluster nodes?” means `kubectl get nodes`; nodes are not split into test/production.
- Labels such as `test-node` or `model` are scheduling metadata, not environment ownership.
- Never classify a node as test or production from labels.

Cluster-scoped writes such as `drain`, `cordon`, `uncordon`, or `taint` require approval because
shared-node changes can affect production Pods. Writes with `-A/--all-namespaces` also require
approval.

## Operating rules

Confirm the target context and namespace before every kubectl command. Prefer explicit context:

```powershell
kubectl --kubeconfig C:/Users/sansm/.kube/qy-online.yaml --context qy-online get pods -n test-inference
```

Read-only production queries are allowed. A real write to `kaic-kis`, `kube-system`, or `default`
pauses for operator approval. A write to `test-inference` does not require Gate approval, but still
requires normal operational care and post-change verification. Never claim success from exit code
alone: run a read-only `kubectl get`/`rollout status`/equivalent check and verify the intended state.

If a command is rejected or times out in approval, do not retry or rephrase it to bypass the Gate.
Report what was blocked and continue only with safe read-only checks. If the Gate returns
`model_fixable`, make the context, namespace, kubeconfig, or referenced manifest explicit before
retrying.

For manifests:

1. Read the complete file first.
2. Confirm every `metadata.namespace`; for `kind: List`, inspect every item.
3. Use `kubectl diff` before apply when supported.
4. Keep `--context`/`--kubeconfig` explicit for writes.
5. Verify rollout/status/events after an approved change.

Do not split, encode, dynamically construct, or wrap the word `kubectl` to evade referral.

## Query guidance

- Node resource usage: prefer `kubectl top nodes --no-headers`.
- Avoid full `kubectl describe nodes` across roughly 110 nodes; describe one node when detailed
  allocated resources are needed.
- Select GPU models with real labels, for example
  `-l accelerator=nvidia-nvidia-b300-sxm6-ac` or `-l nvidia.com/gpu.product=...`.
- Node lists must come from live kubectl output. Never reconstruct truncated output such as
  “5 nodes + and 75 more”.

## GPU idle capacity: two-snapshot local join

For cluster free GPU capacity, use exactly two aggregate Kubernetes reads:

1. `kubectl get nodes -o json` for `nvidia.com/gpu` capacity/allocatable.
2. `kubectl get pods -A -o json` for Pod GPU requests.

Join locally by `spec.nodeName`, with nodes as the primary table and unused nodes counted as
`used=0`. Do not issue one kubectl call per node. Report total capacity, total requested, total
free, and anomalous nodes.
