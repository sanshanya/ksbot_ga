# PROJECT

> `v0.1.0` baseline candidate；正式 tag 等待最终 SHA 的真实 WPS、模型和 Kubernetes 验收。

## 边界与索引

KSBot GA 是 WPS 365 部门级 GenericAgent，不 fork GA。GA 拥有模型、loop、工具、history、working state 和全局 L1/L2/L3/SOP；本项目只拥有 WPS transport、每 chat runtime、交付和 Kubernetes Gate。Shell、网络、文件访问是可信内部能力；只有 `clusters.yaml` 登记的 Kubernetes 写需要 Gate 与人工确认。依赖保持 `ga_wps → ga_core`。

```text
src/ga_core/ga_handler.py  GA 接缝、Gate hook、全局记忆结算
src/ga_core/ga_runtime.py  每 chat Session、loop、workspace、artifact
src/ga_core/gate.py        kubectl 转介、环境取证、AI 裁决
src/ga_wps/protocol.py     canonical message/attachment/mention
src/ga_wps/client.py       WPS auth、API、消息、附件
src/ga_wps/callback.py     callback HTTP 与身份校验
src/ga_wps/history.py      历史、旧附件
src/ga_wps/app.py          调度、实时附件、Bridge、交付、关闭
src/ga_wps/approval.py     单次审批、限时窗口、停止、审计
```

## 运行契约

- Node Bridge 是实时事件唯一真源；Python 只接受 canonical payload。
- 每 chat 一个 Session，同 chat 串行、跨 chat 并行。事件入队或完成控制处理后才记为 accepted；此前失败可重试，此后为 at-most-once。
- 新 Session 最近历史、附件和下载失败统一为 runtime observation。附件按消息摘要与序号隔离；artifact 失败只报告，不重跑 Agent。
- `/stop` 是 chat 级紧急制动：终止当前 Session、清队列，并清除该 chat 全部审批和自动同意窗口。
- 关闭时停止接收、清队列、取消审批、abort 全部 Session，并等待 `GA_WPS_SHUTDOWN_TIMEOUT_SECONDS`；超时由服务入口强制退出。Bridge 暂由 Python 管理。

## Gate 与审批

直接 `kubectl`，或明确执行且内容含 `kubectl` 的本地 `.py/.ps1/.sh/.bash` 脚本进入 Gate。AI 根据完整代码、context/namespace、`KUBECONFIG`、inventory、manifest 和脚本返回 `allow | approval_required | model_fixable`；正则只转介，不追踪 import 链或动态子进程。

`kaic-kis` 写需审批，`test-inference` 写允许；`kube-system`、`default`、all-namespaces 和集群级写受保护。取证或模型失败时 fail-closed，展示原调用和环境，只允许单次批准。

```text
同意 / approve  原调用执行一次
同意 N 分钟     同 chat、同发起人的后续受保护写自动批准
其他回复        取消调用，原文回灌主 Agent
```

窗口内每条调用仍逐条经过成功的 AI Gate；重启或 `/stop` 清除窗口；决定写入 `runtime/approval.jsonl`。

## Skill、记忆、验收

Skill 通过 GA 原生工具调用，不新增 Tool Schema 或 Registry。`wps-chat` 查询历史和旧附件；`ga_wps.history` 是唯一历史实现。GA 全局成长保留，适配层只修正 memory 路径；跨 chat 长期记忆结算串行化，不增加 chat-local durable memory。

运行与检查：`uv sync --extra dev` → `scripts/fetch_ga.py` → `scripts/configure_ga_local.py` → `npm install` → `ga-wps` → contract probe → pytest → ruff → token budget。升级 GA 必须在同一 revision 完成 diff 审查、自动验证和真实闭环。

正式 tag 前验收：WPS 文本；历史附件与 artifact；`test-inference` 可回滚写；`kaic-kis` 单次/限时审批及 `/stop` 后重新审批；跨群全局记忆。Kubernetes 结果必须独立只读验证。

## 限制与负担

Gate 不限制非 Kubernetes 能力、不验证 RBAC；accepted 任务不持久恢复或 replay；Session、审批和窗口是进程内状态；浏览器 driver 沿用上游共享语义。`token_budget.py` 以总量、分组和任务切片作为 ratchet；只有批准的新产品契约可提高。
