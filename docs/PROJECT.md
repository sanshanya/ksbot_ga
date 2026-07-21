# PROJECT

> `v0.1.0` baseline candidate。正式 tag 等待最终 SHA 的真实 WPS、模型和 Kubernetes 验收。

## 产品边界

KSBot GA 是 WPS 365 中的部门级 GenericAgent，不 fork 或复制 GA loop。

```text
GenericAgent  模型、loop、工具、history、working state、全局 L1/L2/L3/SOP
ga_core       GA Handler 适配、每 chat runtime、Kubernetes Gate
ga_wps        canonical WPS transport、调度、附件、审批、交付
```

Shell、网络和文件访问是可信内部 Agent 能力；只有 `clusters.yaml` 登记的 Kubernetes 写操作增加 Gate 与人工确认。依赖单向为 `ga_wps → ga_core`。

## 架构索引

```text
src/ga_core/ga_handler.py  GA module contract、ask_user/code_run/memory/final-reply 适配
src/ga_core/ga_runtime.py  GaChatSession、Agent loop 驱动、workspace 与 artifact
src/ga_core/gate.py        kubectl 转介、环境取证与 AI 裁决
src/ga_wps/protocol.py     canonical event/message 与 mention 数据
src/ga_wps/client.py       WPS auth/API/消息/附件
src/ga_wps/callback.py     callback HTTP 与身份校验
src/ga_wps/history.py      历史分页、解析、渲染与历史附件
src/ga_wps/app.py          chat 调度、实时附件、Bridge 生命周期与交付
src/ga_wps/approval.py     单次审批、限时窗口、停止与审计
```

## 运行契约

Node Bridge 是实时事件格式唯一真源；Python callback 只接受 canonical payload。每 chat 一个 Session，同 chat 串行、跨 chat 并行。事件成功入队或完成控制处理后才记为 accepted；此前失败可重试，此后采用 at-most-once。

新 Session 的最近历史、当前附件和下载失败统一作为 runtime observation。附件写入 `downloads/<message digest>/<index>_<name>`；artifact 失败只报告，不重跑 Agent。`/stop` 可由任意群成员触发，终止当前 Session、清队列并关闭实际任务发起人的审批或窗口。Bridge 在已有外部 supervisor 前由 Python 管理。

## Gate 与审批

直接出现独立 `kubectl`，或明确执行的本地 `.py/.ps1/.sh/.bash` 脚本包含该 token 时进入 Gate。正则只转介；AI 根据完整代码、context/namespace、`KUBECONFIG`、`clusters.yaml`、manifest 和脚本内容返回 `allow | approval_required | model_fixable`。Gate 不追踪 import 链或动态子进程。

`kaic-kis` 写需审批，`test-inference` 写允许；`kube-system`、`default`、all-namespaces 和集群级写受保护。取证或模型失败时 fail-closed，展示原始调用和已知环境，只允许单次批准。

```text
同意 / approve  执行当前原始调用一次
同意 N 分钟     授权同 chat、同发起人的后续受保护写
其他回复        取消当前调用，原文回灌主 Agent
```

窗口内每条调用仍逐条经过成功的 AI Gate；重启或 `/stop` 清除窗口，决定写入 `runtime/approval.jsonl`。

## Skill、历史与记忆

Skill 只由 `SKILL.md` 和可选脚本组成，通过 GA 原生工具调用，不新增 Tool Schema 或 Registry。`wps-chat` 查询历史和下载旧附件；`ga_wps.history` 是唯一历史实现，单次最多 50 条，可扫描全部平台可见历史。

GA 原生全局成长保留：验证事实进入 L2，可复用经验进入 L3/SOP，L1 维护索引；适配层只修正 memory 路径，不增加 chat-local durable memory。`ask_user`、`code_run` 和最终回复只做 WPS/Gate 必需适配；`peer_hint` 不采用隐藏日志扫描。

## 运行与验收

```bash
uv sync --extra dev
uv run python scripts/fetch_ga.py
uv run python scripts/configure_ga_local.py
(cd bridge && npm install)
uv run ga-wps
uv run python scripts/probe_ga_contract.py
uv run pytest -q -ra
uv run ruff check .
uv run python scripts/token_budget.py
```

升级 GA 必须审查 upstream diff，并在同一 revision 执行 probe、pytest 和真实闭环。正式 tag 前完成：WPS 文本；历史附件与 artifact；`test-inference` 可回滚写；`kaic-kis` 单次及限时审批（含 `/stop` 后重新审批）；跨群全局记忆。Kubernetes 结果必须独立只读验证。

## 限制与负担门禁

Gate 不限制非 Kubernetes 能力、不验证 RBAC；accepted 任务不持久恢复或 replay；Session、审批和窗口是进程内状态；GA 记忆和浏览器 driver 沿用上游共享语义。

`token_budget.py` 同时约束仓库总量和典型任务切片（GA Handler/Session、WPS ingress/client/history/service、Kubernetes write）。预算是 ratchet；只有明确新增产品契约时才提高。
