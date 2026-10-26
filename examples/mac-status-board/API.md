# Status Board snapshot API v2

## 0.4.0 主动局域网连接

Mac 的 `telemetry_server.py` 在 TCP/UDP 8642 提供只读数据，使用 Bonjour 服务 `_macboard._tcp` 宣告。设备依次尝试上次验证的地址、mDNS、UDP 发现及有界扫描；扫描仅覆盖当前子网与当前 `/24` 的交集，最多254个地址，且只探测8642端口。流连接期间不扫描，扫描间隔至少60秒，失败以2至60秒退避。网络运行于独立任务，主绘制循环不等网络。

本机已有信息牌后台服务时，OTA上传工具可显式使用 `--pause-agent`：核验并暂停当前用户的固定信息牌LaunchAgent，等待连接结束后上传，在成功、失败、超时、Ctrl-C或SIGTERM退出时恢复。缺省不操作任何服务，兼容其他部署方式。

TCP 采用UTF-8 NDJSON。设备发 `hello`（`v:1`、32位十六进制随机 `nonce`），Mac 回 `challenge`（新随机 `nonce`、`proof`）；设备验证后回 `auth`，Mac验证通过才发 `snapshot`（递增 `seq`、净化后的schema2 `data`）。设备逐帧回相同 `seq` 的 `ack`；Mac仅将最近5秒内确认接收的已验证连接视为活跃。缺失或错误确认会断开，旧HTTP推送自动恢复。`--no-tcp` 保留纯HTTP运行方式。

发往设备的epoch字段采用整数秒，避免设备JSON中间序列化将大浮点时间改为低精度科学计数；小于一秒的采样小数向下取整，不以发送时间替换原采样时刻。主机原始采样和路由数据不受影响。

令牌只作为HMAC-SHA256密钥，从设备NVS及Mac本机私有令牌文件读取，不放在协议、日志或命令参数中。`proof` 对下列ASCII消息求HMAC并转小写十六进制：

```
host:v1:<board_nonce>:<server_nonce>
board:v1:<board_nonce>:<server_nonce>
offer:v1:<board_nonce>:<server_nonce>:8642
```

UDP `probe` 携带设备随机nonce，`offer` 携带服务器nonce、端口及上述签名；连接其实际来源地址，仍须重新完成TCP互认。上次地址和mDNS发现都不能跳过认证。新连接使用新挑战，旧认证不能重放。Mac按真实对端地址只接受RFC1918 IPv4及回环连接；它不提供文件读取、管理或执行命令接口。TCP每帧上限24,576字节，来源仅允许系统、任务标题元数据和额度的固定字段；任务正文及额外字段不会发送。超长展示列表可以裁剪，`total` 保留真实来源总数。

`/status` 增加 `transport`（模式、连接、已验证地址、发现方式、重连/接收/扫描/认证失败计数及数据年龄）、`render_steps`、`render_frames`、`max_step_us`、`total_draw_us`、`loop_max_gap_us` 和 `frame_active`。`progress_target_hz:60` 是两像素进度条的调度目标；用两次读取的 `progress_updates` 差值除以实际间隔验证实际更新率，不将其当作整屏帧率。每个quota回报显示名及窗口百分比/时间，用于确认设备实际收到的内容。`expires_at` 显示为到期，`resets_at` 显示为重置；不会把到期日当作自动补充额度的日期。

共享桥和独立采集均支持 `aliyun-team` / `aliyun-personal` 两项，分别显示团队Credits及个人月度百分比，不加总，也不将团队耗尽状态继承给个人。Kimi最多显示实际提供的四类可靠窗口（短窗、每周、总额度、月度Code）；未提供的窗口不补零。

## 兼容HTTP快照

POST `/stats`, HTTP Basic `admin` + 本机 `private/ota-token`；UTF-8 JSON 最多 24,576 字节。

```json
{
  "schema_version": 2,
  "unix_time": 1791193500,
  "system": {"sampled_at": 1791193500, "host": "Mac-mini", "cpu": 12.5, "memory_percent": 62, "memory_used_gb": 14.8, "memory_total_gb": 24, "disks": [{"name": "Mac SSD", "free_gb": 211, "total_gb": 460, "used_percent": 54}], "cpu_temp_c": 42.1, "gpu_temp_c": 40.2, "fans": [{"name":"fan0", "rpm":1000}], "uptime_hours": 90},
  "tasks": {
    "codex": {"state":"ok", "sampled_at":1791193500, "total":1, "active":1, "items":[{"id":"example", "title":"示例任务", "project":"示例项目", "status":"running", "started_at":1791193000, "ended_at":0, "updated_at":1791193500}]},
    "opencode": {"state":"ok", "sampled_at":1791193500, "total":0, "active":0, "items":[]}
  },
  "quotas": [{"id":"codex", "name":"Codex", "state":"ok", "sampled_at":1791193500, "windows":[{"label":"每周", "remaining_percent":23, "resets_at":1791597920}], "balances":[]}]
}
```

以上是说明性样例，不是当前额度。旧版顶层 cpu/memory/disks 协议仍支持。

- system 字段复用 0.1.4；采样超过15秒过期。unix_time 是 UTC Unix 秒，界面默认跟随 Mac 时区。有效主机时间或 NTP 可校时。
- 每来源最多12条任务。标题最多96个字符、项目36个字符；设备进一步限制 UTF-8 字节长度。状态 running / attention / unknown / completed。条数另报 total / active，不以截断后的条数冒充总量。
- Codex 元数据优先显示 name，回退 title；敏感提示词不作为标题输出。数据库只读，日志增量读取。只看明确状态，超过120秒无事件的运行记录为 unknown；完成保留300秒，待处理保留最近24小时。
- quota 数组由生产者配置，按数组顺序显示；当前五种紧凑别名 id：codex / kimi / ark / deepseek / aliyun，每种最多4个窗口和2笔余额。未提供窗口不补零，余额保留货币单位，默认超过900秒或读取失败显示过期；行带 `valid_until`（绝对epoch）时以其为准判鲜（48小时封顶，离线不会长期冒充实时），无该字段沿用15分钟。
- 0.3.2 起 quota 行可选字段：`name`（显示名，缺失回退id）、`available`（true/false/null，null=未知，绝不判健康也不伪造耗尽）、`stale`、`snapshot:{captured_at,freshness,expired}`、`billing:{reason,monthly_exhausted}`、`valid_until`；窗口内可选 `remaining`/`limit`/`unit`，unit 为 `credits` 的窗口聚合为 Credits 额度卡（最大限额为主池），其余来源维持窗口列表版式。已配置的行即使未知/过期/耗尽也保留显示。
- 上游读取失败保留原 sampled_at，禁止用重试时间更新数据年龄。
- GET `/status` 返回版本、联网/主机、时间、page(0系统/1Codex/2OpenCode/3起为额度页，`page-2` 即额度页码)、`page_count`、`quota_pages`、paused、display_buffer、free_heap、各来源采样时间和新鲜度。
- 串口115200：v2快照可直接按行发送；原 stats/status/provision/panel 命令保留。超长串口行整行丢弃，防止后半段被误认为新命令。
- `/update` 延续原认证和双分区 OTA；本次构建不改分区或已存配网信息。

### 0.2.1 绘制诊断

`/status` 增加 `draw_us`（最近整页绘制耗时）、`max_draw_us`（运行期间最大耗时）、`progress_us`（最近进度更新检查耗时，含无变化检查）、`progress_updates`（实际进度条写屏次数）、`progress_target_hz`（目标检查频率 20Hz）。这不是全屏 20 FPS；系统数据仍按原频率采集，秒钟和内容按秒或数据变化刷新。

### 0.2.2 轮播与采集

系统页 18000ms、任务页 15000ms、额度页各 8000ms。`/status` 增加 `page_duration_ms` 和 `opencode_count`。OpenCode 只保留明确 `running` 且最近有状态观测的记录，待处理、完成和过期无法确认的记录不纳入显示计数。系统采集周期2秒，快照发布周期1秒，原过期阈值不变。

### 0.3.0 时区和设置

快照增加 `utc_offset_seconds`（Mac 当前当地时间相对 UTC 的秒偏移，整数，范围 -50400..50400，含夏令时）。缺失或无效时保留上次有效偏移，首次默认 UTC+8。主机每次发送重新读取系统时区。`/status` 增加 `utc_offset_seconds`、`display_utc_offset_seconds`、`follow_mac_timezone`、`brightness_percent`、`menu_open`、`menu_info`、`menu_selected`。时间与过期计算仍使用 UTC 秒。时区选择、最近 Mac 偏移、亮度和用户轮播暂停状态保存于 NVS，不保存菜单打开状态。

### 0.3.1 磁盘和模型

系统 `disks` 最多3项：Mac数据卷及最多两块外接挂载卷。`free_gb` 和 `total_gb` 延用旧字段名，数值单位为 GiB（除以1024^3）。OpenCode task新增 `model` 显示字符串，来自实际观测的 `actual_models`，多模型明确标注；缺失显示模型未知。`/status` 增加 `disks`（名称及容量）和 `tasks.opencode.models`，用于核对设备接收到的显示数据。

### 0.3.2 动态额度分页与阿里百炼

额度页数量随 payload 中配置的来源数动态生成：每页两项，四种来源两页、五种三页（奇数末页第二格留空）；来源数组为空时隐藏全部额度页，其余页序与轮播时长不变（系统18秒、任务页各15秒、每页额度8秒）。设备 `/status` 的 `quotas` 对象按收到的 id 动态输出 `sampled_at`、`fresh`、`state`、`available`/`exhausted`、`stale`、`monthly_exhausted` 与 `credits`（remaining/limit/unit/resets_at），用于核对实机接收数据；另增 `page_count`、`quota_pages`。带 `valid_until` 的行按生产者声明的绝对有效期判鲜；无该字段沿用15分钟。手动导入的快照显示「快照」来源标注，不冒充实时接口读数。运行主机端优先转发共享桥 payload，共享文件存在期间本地额度采集器不启动，避免重复账户 API 调用。
