# PROJECT

> 当前状态：pre-baseline candidate。自动测试通过不等于真实 WPS/模型/Kubernetes 闭环通过；完成文末真实验收前不建立 baseline tag。

## 目标与边界

在 WPS 365 中嵌入上游 GenericAgent（GA），提供对话式 AI 与 Kubernetes 运维门禁。不 fork GA。

产品拥有：WPS transport、每 chat Agent、工作区交付、`do_code_run` Gate 钩子。GA 拥有：模型客户端、loop、dispatch、history、工具和 UI。

## 结构

```text
src/ga_core/  GA 适配、Gate、通用配置、Skill 索引
src/ga_wps/   WPS 配置、协议、审批、调度和 UI 入口
```

依赖单向：`ga_wps → ga_core`。`ga_core` 不 import `ga_wps`；只定义最小 `ApprovalSink` Protocol。`vendor/GenericAgent/` 是由 `GA_REVISION` 固定的独立上游 Git checkout。

## 信任模型

KSBot GA 是部署在专用线上容器中的高能力内部 Agent，不是多租户沙箱。能够调用机器人的 WPS 成员被视为可信内部操作者；容器按实际任务挂载 kubeconfig、项目、日志和模型目录。

任意 Python、PowerShell、Bash、网络和绝对路径文件访问是产品能力。chat workspace 只组织历史、附件和产物，不是权限边界。产品只对明确登记的生产 Kubernetes 写操作增加 Gate 与人工确认；其他环境保持 Agent 自主执行。

## 消息与并发

WPS 事件经 Node bridge 归一化后进入 callback。当前 App 的 mention 只设置 `mentioned=true`，不进入业务正文；其他 mention 保留明确空格边界。群聊回复使用事件中的真实 `sender_id` 作为 mention identity，并通过通讯录接口缓存真实姓名；无 `kso.contact.read` 权限时仍使用 user ID 与回退标签发送。`event_id` 先经过有限窗口去重，窗口保存在内存和 `runtime/seen_events.jsonl`。

每 chat 一个 `GaChatSession`。同 chat 串行，跨 chat 由线程池并发。每条 WPS 事件先更新当前 workspace 的 `.wps_context.json`（chat 身份、当前事件和已知显示名，不含凭证）。消息处理为：当前消息附件下载 → 首次会话执行 `wps-chat` Skill 脚本生成透明 bootstrap observation → GA loop → Markdown 回复和 artifacts 上传。

`/stop` 优先于审批回复：取消当前发起人的等待审批、调用 GA `abort()`、清空该 chat 尚未处理的队列。

## Kubernetes AI Gate

Gate 只挂在 `RuntimeAgentHandler.do_code_run`：

- 代码直接含独立 `kubectl`/`kubectl.exe` token；或
- 代码明确执行本地 `.py/.ps1/.sh/.bash` 文件，且完整脚本含该 token。

只识别明确脚本入口，不分析 Python AST、import 链或通用子进程语义。动态脚本路径无法完整读取时返回 `model_fixable`。正则只做转介，AI 是唯一语义裁决者。

事实源：

- `config/clusters.yaml`：唯一环境与写策略真源；
- 当前 kube context、namespace、`KUBECONFIG`；
- `-f/--filename` manifest 和明确执行的本地脚本。

引用文件必须完整读取；超出 `gate_input` 数量或大小限制时不截断裁决。Gate 输入、审批等待中的调用和最终执行使用同一完整代码。

Gate 只返回：

```json
{"decision":"allow|approval_required|model_fixable","message":"自然语言审查"}
```

`approval_required` 的 `message` 说明命令作用、真实目标、可能影响和审批原因，不复制原始代码。

环境在 namespace 层：

- `kaic-kis`：production，写操作需审批；
- `test-inference`：test，写操作允许；
- `kube-system`、`default`：非生产但写保护；
- 未列出 namespace：视为非生产，写操作允许；需要保护的 namespace 必须显式登记；
- all-namespaces 和集群级写：需审批。

inventory、模型或 Gate 调用失败时 fail-closed 为 `approval_required`。

## 审批

WPS 审批绑定当前任务发起人：回复“同意”（或 `approve`）只执行当前调用；回复“同意5分钟”等明确时长，可执行当前调用并为同一 chat、同一发起人开启限时自动同意。窗口内每条操作仍先经过 AI Gate，只有 AI 成功分类出的 `approval_required` 可复用授权；`model_fixable` 或 Gate/inventory/model 失败均重新询问。其他回复取消当前操作并回灌主模型，其他成员和其他 chat 不能使用窗口。`/stop` 终止任务并关闭该发起人的窗口；服务重启也会清空窗口。审计记录首次授权、窗口到期时间和每次自动批准。

本地 UI 保持单次审批。由于上游 UI 会结束当前工具调用，本地 UI 使用一次性内部调用哈希，只允许完全相同的命令重试一次；该哈希不展示给用户，也不参与 WPS 审批窗口。

## WPS 历史、附件与重启

WPS 历史不是隐藏宿主能力，也不发布成新的模型 Tool。`skills/wps-chat/scripts/wps_chat.py` 是领域能力入口，GA 使用固定基础工具 `code_run` 调用它：

- `history`：读取当前群最新消息，可按参与者或关键词筛选；
- `download-latest`：下载最近历史消息中的附件；
- `download --message-id ...`：下载指定历史消息附件。

脚本只从 workspace 的 `.wps_context.json` 获取当前 chat 身份，模型不传 chat ID 或凭证。历史解析、分页、筛选和渲染由 `ga_wps.history` 单一实现；首次会话 bootstrap 直接调用它，Skill 脚本只是 CLI 入口，并把来源、获取时间、范围和刷新命令注入 `<bootstrap_observation>`。

进程重启后不恢复 GA checkpoint 或 backend history；新 Session 再次运行该 Skill 脚本。单次结果最多返回 50 条，但脚本可分页扫描全部 WPS 可访问历史，以满足最新消息、筛选和历史附件查询。Skill 和 bootstrap 均要求严格 UTF-8；真实中文乱码仍必须用脱敏 WPS 响应逐层定位，禁止猜测式转码。

GA 原生 `start_long_term_update` 的全局专业成长语义保留：行动验证的环境事实进入共享 L2，高成本可复用经验进入共享 L3/SOP，并维护 L1 索引。适配层只把 memory 路径绑定为 pinned GA checkout 下的绝对路径。`session_memory.md` 仍可由文件工具保存仅该群有效的稳定事实，但不是聊天记录，也不得替代全局记忆。

真实验证：配置 WPS 凭证和 `WPS_HISTORY_TEST_CHAT_ID` 后执行：

```bash
uv run pytest -q -s tests/test_wps_live.py
```

## GA 行为适配

当前 GA 行为契约：

| 上游行为 | 当前决定 | 原因 |
|---|---|---|
| System Prompt 与全局记忆 | 保留 | 部门级 Agent 跨群成长 |
| `ask_user` | 适配 WPS 中断 | transport 必需 |
| `code_run` | 增加 Kubernetes Gate | 只保护登记范围内的写操作 |
| `start_long_term_update` | 保留原生语义，仅适配绝对路径 | 共享 L1/L2/L3/SOP |
| per-chat memory 替代全局 memory | 禁止 | 会破坏跨群专业成长 |
| `peer_hint` | 不采用 | 上游临时日志扫描不是正式跨群接口；进行中任务查询不属于本基线 |
| `verbose` | WPS loop 关闭 | 避免无效内部输出，不影响工具能力 |
| 最终回复 | 映射 WPS callback | transport 必需 |

不得用 per-chat memory、Prompt 禁令或 transport 状态替代 GA 原生全局成长能力。未来若需要跨群进行中任务查询，应发布明确的 Skill/脚本，而不是恢复隐藏日志扫描提示。

## 上游契约

`scripts/probe_ga_contract.py` 检查固定 GA 的关键符号。测试在本地存在 pinned checkout 时，使用真实 `GenericAgentHandler` 和 `StepOutcome` 验证 Handler 组合、inline eval 与 `abort()`。

升级流程：修改 `GA_REVISION` → `scripts/fetch_ga.py` → 查看上游 diff → contract probe → pytest → 阶段提交。

`fetch_ga.py` 保留完整提交图，拒绝 dirty checkout，并保护 `mykey.py`、`memory/vision_api.py` 和本地 memory 文件。

## 配置与发布

`vendor/GenericAgent/mykey.py` 是 GA 默认模型配置位置；Gate 默认通过 `GA_GATE_CONFIG_KEY` 复用该配置。`scripts/configure_ga_local.py` 默认拒绝覆盖已有 `mykey.py` 或 `vision_api.py`，只有显式 `--force` 才替换。

包名为 `ksbot-ga`。父仓库采用 MIT License；独立 checkout 的 GenericAgent 保留其上游 MIT 版权与许可。

Vision 仍是可选模板入口，需要人工选择视觉模型并配置 `OPENAI_CONFIG_KEY`、backend 和 endpoint。

## Skill

Skill 是 AI 可发现的能力包：`SKILL.md` 说明何时和如何使用，必要时可附带脚本。脚本由 GA 固定基础工具 `code_run`/`file_read` 调用；Skill 不向模型新增 Tool Schema，不修改 dispatch 或 Agent loop。`skills.py` 只扫描 `skills/*/SKILL.md` frontmatter 生成索引；无 Registry、activation lifecycle 或第二份清单。`clusters.yaml` 是环境事实唯一源。

## 认知负载门禁

```bash
uv run python scripts/token_budget.py
uv run python scripts/token_budget.py --json
```

脚本按 `core.runtime`、`core.gate`、`wps.protocol`、`wps.history`、`wps.service`、`wps.approval`、`wps.bridge`、`wps.ui`、`wps.skill-cli`、分组测试等功能模块记录文件与模块的估算 LLM token 数。每个单模块硬上限为 30000 token。估算规则固定为：CJK 字符或标点各 1 token，ASCII 连续串每 4 字符约 1 token；它用于稳定比较认知负载，不冒充任何模型的计费 tokenizer。

新增功能不得通过拆文件、压行或移动目录规避预算；拆分必须对应可独立理解的功能边界，未归属的维护文件直接失败。评审同时检查模块 token 变化、职责数量和重复执行路径。

## 基线前真实验收

单元测试和 contract probe 只证明局部契约。首个“已验证工程基线”还必须在目标环境完成以下真实闭环，并把时间、chat、命令和结果记录在阶段提交说明中：

1. 真实 WPS 文本消息 → pinned GA 与真实模型 → WPS 正常回复；
2. 真实 WPS 附件（附件与指令分开发送）→ Agent 读取 `wps-chat` Skill → 通过 `code_run` 下载并读取 → 生成 artifact → WPS 回传；
3. `test-inference` 可回滚写操作 → Gate `allow` → 实际执行并验证结果；
4. `kaic-kis` 可回滚写操作 → Gate 说明作用与影响 → “同意”单次执行；另以“同意5分钟”验证窗口内连续写操作不重复询问、仍逐条经过 Gate，并在 `/stop` 后失效；
5. 群 A 的行动验证经验经 `start_long_term_update` 进入 GA 全局 L2/L3/SOP，群 B 能发现并复用，且临时状态未被写入。

未全部完成时只能称为 pre-baseline candidate，不得建立正式 baseline tag，也不得把 Agent 自述“成功”当作验收证据。Kubernetes 写操作必须由独立只读命令确认真实状态。

## 运行

```bash
uv sync --extra dev --extra ui
uv run python scripts/fetch_ga.py
uv run python scripts/configure_ga_local.py --local
# 编辑 vendor/GenericAgent/mykey.py
(cd bridge && npm install)
uv run ga-wps
```

## 已知限制

- Gate 只保护 Kubernetes 操作，不限制其他任意 Python、PowerShell、Bash 或文件能力；
- 外部脚本只检查明确执行入口，不追踪 import 或动态子进程；
- kube 探测只读取 context/namespace，不验证实际 RBAC；
- 审批状态和限时窗口在内存中，进程重启时取消，但审计保留；
- WPS 单次结果最多 50 条；Skill 可分页扫描全部平台可访问历史，但无法读取平台未授权的早期消息；
- 浏览器 driver 仍是上游全局实例；发生真实并发冲突后再加锁；
- 尚未实现 Skill proposal 与人工 promotion。
