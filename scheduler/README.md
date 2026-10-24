# 可选外部调度

唯一用途：Cloudflare Workers Cron Trigger → GitHub Workflow Dispatch → main 的既有 monitor 模式。Worker 不读取 X/DeepSeek/SMTP 凭据，不执行监控或保存游标，也没有公开 HTTP 触发入口。

wrangler.toml 的 crons=[]、ENABLED=false 是刻意保留的停用配置；R1 不改变 GitHub 原生 cron。请求超时 10 秒，接受当前 API 的 HTTP 200 及旧版 204，其他状态及网络异常明确失败，不输出响应正文。POST 不自动重试，避免接受后丢失响应引起即时重复；下个 Cron 再尝试。HTTP 接受不证明监控 job 成功或邮件送达。

## 请求兼容性与故障诊断

Cloudflare Workers 不支持 `redirect: "error"`，会在发出 HTTP 请求前抛出 TypeError。使用 `manual` 并仅接受 200/204；3xx 明确失败，不跟随 Location，避免将认证信息发送到重定向目标。参见官方 [Request 文档](https://developers.cloudflare.com/workers/runtime-apis/request/)。R3-D 同一 Worker 的远端预览 GET 对照：error 模式在 0ms 内失败，manual 模式返回 GitHub 200；保持 AbortSignal.timeout(10000)、User-Agent 和 API 版本不变。

每次 scheduled 执行都记录 scheduled_at、observed_at、elapsed_ms、http_status、exception_name、aborted 和 status。状态包括 REQUEST_TIMEOUT、NETWORK_ERROR、FETCH_RUNTIME_ERROR、DISPATCH_HTTP_4XX、DISPATCH_HTTP_5XX、DISPATCH_HTTP_UNEXPECTED、DISPATCH_ACCEPTED；未配置 Secret 明确为 DISPATCH_TOKEN_MISSING。异常名称使用固定白名单，不输出上游异常信息、堆栈、响应正文、请求对象或 Token。NETWORK_ERROR 不等于已确认 DNS/TLS 故障，超时也不能证明 POST 未被接受；失败仍抛出脱敏异常，使 Worker outcome 保持失败。

仓库配置是安全停用模板，不代表线上实际配置。R3 使用本机 `.git/tibo-r3-wrangler.toml` 保留启用变量、Cron 与执行日志；部署前核对 Cloudflare 当前版本、绑定名称和触发器，避免用模板误停生产。默认正式部署保持 workers_dev=false、preview_urls=false，无公开 HTTP 触发入口。诊断只读预览不包含 Dispatch POST，验证后关闭预览；不要把诊断入口留在正式 Worker 中。

## 部署前置条件（仅在 Supervisor 授权后执行）

1. R1 已审查合入 main、离线测试通过；确认 Cloudflare 账户及计划限制。
2. 人工创建有到期日的 GitHub fine-grained PAT：资源所有者选仓库所有者，Repository access **仅选 duckzhou2/tibo-x-monitor**；仅授予 **Actions: write** 和 GitHub 自动要求的 Metadata: read，不授予 Contents write。GitHub 不能进一步把此权限限定为单一 workflow，需要认可其仓库内 Actions 权限范围并定期轮换。
3. 仅在 Cloudflare 部署环境创建 Secret binding `GITHUB_DISPATCH_TOKEN`，通过安全输入注入，不写入 TOML、仓库、聊天或命令行参数。不配置任何 X/DeepSeek/SMTP Secret。
4. 用 Cloudflare 控制台或已批准的 Wrangler 安装部署本目录模块，先保持 Cron 为空、ENABLED=false；R1 不安装部署依赖。
5. 授权真实验收后设置 ENABLED=true，把 Cron 配成 `7,37 * * * *`（UTC），观察实际 dispatch、Actions、邮件和 state。配置传播可能延迟，不承诺严格半小时 SLA。
6. 外部触发验收稳定后，再单独授权移除 GitHub 原生 schedule 条目。不要禁用整个 workflow，否则 dispatch 也会失效。短期双调度会多做空轮询；全局 concurrency 和 job 开始时读取最新 main 防止同时用旧状态发送。GitHub 可能替换尚未执行的 pending job，concurrency 不是持久化事件队列。

外部调度只改善事件来源，不消除 GitHub runner 或 SMTP 故障。仍需人工定期核查 Cron 结果、Token 到期和 Actions 历史；本次不搭建独立告警平台。

## 免费方案与真实频率验收

按当前官方 [Limits](https://developers.cloudflare.com/workers/platform/limits/) 和 [Pricing](https://developers.cloudflare.com/workers/platform/pricing/)，Workers Free 支持 Cron，账户最多 5 个触发器，每天 100,000 次请求、每次 10ms CPU。每半小时一次只需要 1 个触发器、每天约 48 次调用，每次 1 个外部子请求；等待网络不计 CPU。这一规模可使用免费方案，但账户其他用量、实际 CPU 与配额必须在授权部署时核实，离线 Node 测试不能证明 Cloudflare CPU 耗时或真实调度可靠性。

启用后至少观察完整 48 小时：

1. Worker 成功日志有 `status`、`scheduled_at`、`observed_at`（UTC ISO 时间），按计划时间每天核对 48 个时槽，统计丢槽、失败及实际相邻间隔的中位数、P95、最大值。超过 60 分钟的空档需解释，不能只看总成功数。
2. 对照 GitHub 的 `workflow_dispatch` 运行数、created_at、run_started_at、结论和摘要。HTTP 接受、运行创建、实际执行及通知成功是四个不同判据。双调度期间分开统计 schedule 与 workflow_dispatch。
3. 核对失败、取消、pending 替换和重复邮件；只在没有未解释缺口、SMTP/状态错误时请求 Supervisor 批准移除原生 schedule。之后再次观察 48 小时。移除 schedule 条目不影响保留的 workflow_dispatch；不要点击 Disable workflow。
4. 发生 Worker 错误、Token 到期、未解释调度缺口或 GitHub runner 故障时，按下面的回退步骤恢复原生触发并保留现场日志；不要回退游标或靠反复手动补发掩盖故障。

## 回退

先恢复 GitHub 原生 schedule（如已移除），再设置 Worker ENABLED=false 并移除 Cron。确认原生运行恢复后按授权撤销 Token。无需改 state 或业务代码，不要删除正式状态。

## 离线验证

仓库根目录执行 `node --test scheduler/worker.test.mjs`，fetch 全部注入 Mock，不需要 npm 包或 Cloudflare 账户。

参考：[Cloudflare Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/)、[GitHub Workflow Dispatch 与权限](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)。
