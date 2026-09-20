# 路由与套餐利用率

[English](routing.md)

优先分配完整任务、利用可用套餐，同时满足模型能力和上下文需求。固定订阅已经支付，
DeepSeek 直连会消耗余额。不要为了切换渠道，把一个任务拆碎或重复发送整个代码库。

## 方舟 Agent Plan

通过 OpenCode 的私有凭据存储连接 `volcengine-agent-plan`。Key 不进入任务、命令参数、
仓库或报告。添加对应 Profile 后，Worker Desk 自动提供 Responses API 配置：

| Profile 示例 | 模型 | 上下文 / 最大输出 | 思考强度 |
| --- | --- | --- | --- |
| `ark-auto` | `volcengine-agent-plan/ark-code-latest` | 256,000 / 32,000 | `max` |
| `ark-k3` | `volcengine-agent-plan/kimi-k3` | 1,024,000 / 65,536 | `max` |

SDK 为 `@ai-sdk/openai`，套餐接口为 `https://ark.cn-beijing.volces.com/api/plan/v3`。
凭据放在 OpenCode 私有 `auth.json` 的对应 Provider 下，运行配置不包含 Key。
不会更改交互式 OpenCode 的默认模型。

`ark-code-latest` 跟随方舟控制台的选择，后台设为 **Auto** 才启用自动路由。
不能把它当作保证使用 K3 或保证 1M 上下文的别名。两种模型均于 2026-09-20 实测接受
`reasoning.effort=max`。

2026-09-20 核对的官方规则：Auto 抵扣系数 **0.5**，活动至 **2026-11-08**；固定 K3
系数 **10**。相同输入、输出 Token 下，固定 K3 消耗的 AFP 是 Auto 的 **20 倍**。
这比较的是套餐额度，不代表质量、速度或最终金钱成本等价。官方还说明夜间
00:00–08:00 会提高 Auto 的 K3 路由比例，但不保证每次选中。
请以[现行抵扣规则](https://www.volcengine.com/docs/82379/2516283)和
[活动期限](https://www.volcengine.com/docs/82379/2533565)为准；调度器不会写死临时折扣。

推理 Key 不能直接查询 AFP 余量。方舟控制面的 `GetAFPUsage` 需要另行授权的账号
AccessKey ID/Secret。目前没有接入该适配器时，额度标为未知，执行报错仍返回协调模型。
不会伪造余额或自动收集其他凭据；接入也不会开启方舟的超额后付费。

来源：[OpenCode 接入文档](https://www.volcengine.com/docs/82379/2373741)、
[套餐概览](https://www.volcengine.com/docs/82379/2366394)、
[CC Switch 用量适配器](https://github.com/farion1231/cc-switch/blob/main/src-tauri/src/services/coding_plan.rs)。

## 顺序路由与加权分配

在 `~/.config/opencode/delegate-pool.json` 设置可选 `routing_policy`。
完整 JSON 示例见[英文说明](routing.md#ordered-stages-and-weighted-pools)。
每个任务层级有多个有序阶段，每个阶段可包含多个带权重的 Profile：

- 普通任务：Kimi → 方舟 Auto → DeepSeek。
- 深度任务：可选 Kimi K3 → 方舟 K3 → DeepSeek，或者把两路 K3 放在同一阶段按 2:1 分配。
- 显式选择 Profile 会固定模型，绕过策略；通常使用 `--profile auto`，按任务性质选择 `--complexity deep`。

调度器只使用第一个存在可用模型的阶段。某路不可用时由可用成员接替。
2:1 统计的是实际派发的任务数，不是 Token、花费或正在运行的任务数。
查看状态、等待容量或文件冲突不会消耗轮次；计数重启后保留。
未配置的层级沿用原单 Profile 路由，空策略恢复原路由。修改运行配置应在任务空闲时应用。

追求套餐效率时，建议普通任务使用方舟 Auto 接替，深度任务优先 Kimi K3，
方舟固定 K3 留作深度任务备用。精准优化还需要各套餐实时余量和刷新时间；额度未知不等于无限。
让同一任务保留在已选模型上，复用有用上下文，给主模型返回精炼证据。
运行失败时由协调模型检查部分结果，再选模型继续剩余工作；桥接层不会盲目重放已接受任务。
