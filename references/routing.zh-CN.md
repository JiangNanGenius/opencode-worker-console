# 路由与套餐利用率

[English](routing.md)

优先分配完整任务、利用可用套餐，同时满足模型能力和上下文需求。订阅套餐已经支付，DeepSeek
直连余额属于增量支出。不要为了换渠道拆碎任务，也不要重复发送整个上下文。

## 方舟 Agent Plan

通过 OpenCode 的私有凭据存储连接 `volcengine-agent-plan`。Key 不进入任务、仓库、命令参数或
报告。启用对应 Profile 后，Worker Desk 自动提供 Responses API 配置：

| Profile 示例 | 模型 | 上下文 / 最大输出 | 思考强度 |
| --- | --- | --- | --- |
| `ark-auto` | `volcengine-agent-plan/ark-code-latest` | 客户端上限 1,024,000 / 32,000 | `max` |
| `ark-evolving` | `volcengine-agent-plan/doubao-seed-evolving` | 1,024,000 / 65,536 | `max` |
| `ark-k3` | `volcengine-agent-plan/kimi-k3` | 1,024,000 / 65,536 | `max` |
| 仅手动选择 | `volcengine-agent-plan/deepseek-v4.1-flash` | 1,024,000 / 65,536 | `max` |

SDK 为 `@ai-sdk/openai`，套餐 Base URL 为
`https://ark.cn-beijing.volces.com/api/plan/v3`。推理 Key 仍由 OpenCode 私有存储，生成的运行
配置不含 Key。`ark-code-latest` 跟随方舟控制台选择，模型身份不够确定；需要明确能力时使用
固定 Evolving 或 K3。1,024,000 客户端上限用于避免 OpenCode 在 256K 提前压缩，实际后端限制
仍以当次模型为准。

AFP 查询使用另一组火山控制面 AK/SK。按[凭据说明](credentials.md)注册两个元数据引用后，
Worker Desk 在进程内签名固定的 `GetAFPUsage` 与 `GetPersonalPlan` 请求，只返回套餐和窗口信息。
未配置时仅显示额度未知，不影响推理。四个额度窗口和所有方舟模型共用同一 AFP 续航信号。

来源：[OpenCode 接入](https://www.volcengine.com/docs/82379/2373741)、
[套餐概览](https://www.volcengine.com/docs/82379/2366394)、
[GetAFPUsage](https://www.volcengine.com/docs/82379/2479847)、
[GetPersonalPlan](https://www.volcengine.com/docs/82379/2546382)。

## 默认套餐优先策略

全新安装使用 `--preset ark-agent-plan` 会建立：

- 快速任务：Ark Auto → DeepSeek 直连。
- 普通任务：原厂 Kimi K2.8 : 方舟 Seed Evolving = 1:1 → Ark Auto → DeepSeek。
- 深度任务：原厂 Kimi K3 : 方舟 K3 = 2:1 → 普通任务组合 → Ark Auto → DeepSeek。

调度器只使用第一个存在可用模型的阶段。显式 Profile 固定模型并绕过策略。比例统计的是实际
派发任务数，不是 Token 或花费；等待会话并发、文件锁或额度恢复不会消耗轮次。

Kimi 是经济性基准，因为同一个 Kimi 模型经方舟 AFP 购买通常更贵。实时、可用、带重置时间的
额度数据只允许把比例移动一档：

| 组合 | 基准 | 允许范围 |
| --- | --- | --- |
| 原厂 Kimi K3 : 方舟 K3 | 2:1 | 3:1、2:1、1:1；方舟 K3 不会超过原厂 |
| 原厂 Kimi K2.8 : 方舟 Evolving | 1:1 | 2:1、1:1、1:2 |

每个窗口的续航信号为“剩余额度比例 / 距离重置的剩余时间比例”，取最紧张窗口。数据过期、缺失或
未认证时保持基准。Ark Auto、Evolving 和 K3 共用一个信号，因此 Auto 多用会自动压低后续方舟
占比。接口明确为零时移除该渠道。已派发任务保持原模型，失败后由协调模型检查部分工作再选择。

## AFP-equivalent

Worker Desk 把成本假设和真实额度分开保存，默认值可在控台修改：

```text
1 AFP-equivalent = ¥0.002
¥1 = 500 AFP-equivalent
Kimi ¥699 = 349,500 AFP-equivalent（仅表示购买成本的名义等价值）
```

这不代表 Kimi 官方提供 349,500 AFP，也不能凭它推算固定 Token 额度。若一个完整 Kimi 账期实际
处理 `M` 百万 Token，`349500 / M` 只能得到该账期的混合名义成本；没有分模型明细或多组不同配比
样本，就不能分别反推出 K3 与 K2.8。

按可配置假设 Auto=50、Evolving=250、方舟 K3=1000 AFP/百万 Token，以及 K3 2:1、第二档 1:1：

```text
Ark AFP = 333.33D + 125N + 50S
```

`D/N/S` 是深度、普通、小任务的百万 Token 数。动态比例下为
`1000*rD*D + 250*rN*N + 50*S`，其中 `rD` 只能为 1/4、1/3、1/2，`rN` 只能为
1/3、1/2、2/3。系数和活动会变化；方舟接口返回的实时 `Used` 才是实际依据，控台会把实际值、
估算值和名义人民币成本分开显示。
