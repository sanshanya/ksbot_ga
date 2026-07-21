# Agent Project Rules

编码前创建、结束后删除 `.agent/TASK`：

```text
OUTCOME: 可观察结果
NON-GOALS: 不做什么
VERIFY: 实际验证
ADDS: 新增永久概念，或 none
```

无法明确 `OUTCOME` 与 `VERIFY` 时不得编码。

优先删除、复用、修改；仅在结果无法实现时新增永久文件、API、配置、依赖或运行模式。禁止假想扩展、兼容层、备用路径、单实现抽象、新旧并存、压行和把逻辑移入 Prompt。同一行为只留一条路径；拆分必须降低真实任务切片。改变 GA 原生行为前更新 `PROJECT.md`，削弱核心能力须经用户批准。

`PROJECT.md` 只写当前边界、索引、契约、验证和限制；历史证据进入 Release、commit 或运行审计。一个错误行为只留一个最强测试；等价输入参数化；避免绑定私有容器、精确文案和无产品意义的中间值。Mock 不替代真实 WPS、模型和 Kubernetes 验收。

阶段提交前执行 `VERIFY`、pytest、ruff、token budget、diff 检查并删除临时内容、重复路径和可删代码。总量与任务切片都是 ratchet；增长必须对应批准的新产品契约。
