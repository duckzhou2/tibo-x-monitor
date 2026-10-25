# Cloudflare 定时调度（已上线）

唯一用途：Cloudflare Workers Cron Trigger → GitHub Workflow Dispatch → main 的既有 monitor 模式。Worker 不读取 X/DeepSeek/SMTP 凭据，不执行监控或保存游标，也没有公开 HTTP 触发入口。

**当前正式配置**（2026-10-25）：Cloudflare Worker 已启用，Cron 为 `7,37 * * * *`（UTC）；GitHub 原生 schedule 同样保留，这是经用户接受的双调度长期运行方式。仓库 `wrangler.toml` 的 `crons=[]`、`ENABLED=false` 刻意保留为**停用安全模板**，不代表线上配置，不能直接用它覆盖生产部署。

Worker 请求上限 10 秒，接受 GitHub 200/204，其他状态或网络异常明确失败；POST 不自动重试，避免接受后丢失响应引起即时重复。HTTP 接受不证明 GitHub Job 成功或邮件送达。

## 请求兼容性与故障诊断

Cloudflare Workers 不支持 `redirect: "error"`，会在发出 HTTP 请求前抛出 TypeError。使用 `manual` 并仅接受 200/204；3xx 明确失败，不跟随 Location，避免将认证信息发送到重定向目标。参见官方 [Request 文档](https://developers.cloudflare.com/workers/runtime-apis/request/)。R3-D 同一 Worker 的远端预览 GET 对照：error 模式在 0ms 内失败，manual 模式返回 GitHub 200；保持 AbortSignal.timeout(10000)、User-Agent 和 API 版本不变。

每次 scheduled 执行都记录 scheduled_at、observed_at、elapsed_ms、http_status、exception_name、aborted 和 status。状态包括 REQUEST_TIMEOUT、NETWORK_ERROR、FETCH_RUNTIME_ERROR、DISPATCH_HTTP_4XX、DISPATCH_HTTP_5XX、DISPATCH_HTTP_UNEXPECTED、DISPATCH_ACCEPTED；未配置 Secret 明确为 DISPATCH_TOKEN_MISSING。异常名称使用固定白名单，不输出上游异常信息、堆栈、响应正文、请求对象或 Token。NETWORK_ERROR 不等于已确认 DNS/TLS 故障，超时也不能证明 POST 未被接受；失败仍抛出脱敏异常，使 Worker outcome 保持失败。

仓库配置是安全停用模板，不代表线上实际配置。R3 使用本机 `.git/tibo-r3-wrangler.toml` 保留启用变量、Cron 与执行日志；部署前核对 Cloudflare 当前版本、绑定名称和触发器，避免用模板误停生产。默认正式部署保持 workers_dev=false、preview_urls=false，无公开 HTTP 触发入口。诊断只读预览不包含 Dispatch POST，验证后关闭预览；不要把诊断入口留在正式 Worker 中。

## 正式部署与凭据维护

1. **优先保持现有 Worker 运行，不需重新部署。**若必须维护，先检查 Cloudflare 中实际 Worker 版本、`ENABLED`、Cron、Secret 绑定，再对照本机 `.git/` 下保留的 R3 部署配置。仓库中的停用模板不能直接作为生产配置上传。
2. GitHub Fine-grained PAT 仅授权 `duckzhou2/tibo-x-monitor` 仓库的 **Actions: write**（及 GitHub 要求的 Metadata: read），设置到期日并按需轮换。GitHub 不能把这项权限进一步限定到单个 Workflow。
3. 仅通过 Cloudflare 安全 Secret 绑定 `GITHUB_DISPATCH_TOKEN`；不得写入仓库、日志、聊天或配置文件，不需要把 X/DeepSeek/SMTP Secret 交给 Worker。
4. 当前 Worker 保持 `workers_dev=false`、`preview_urls=false`，无公开 HTTP 触发入口。更改配置后同时验证 Cloudflare Cron → GitHub `workflow_dispatch` → monitor → state 链路；不要仅以 HTTP 200 判断全链路成功。
5. 当前 **不计划移除 GitHub 原生 Cron**。若以后主动决定切换单一调度，先评估取消／并发风险并完成受控验证；不得 Disable 整个 GitHub Workflow（否则 Dispatch 也不可用）。

外部调度只改善触发时间来源，并不能消除 GitHub Runner、X、SMTP 或凭据故障。额外健康巡检已停止；在缺乏邮件或运行记录时，应人工查阅 GitHub Actions 与 Cloudflare 日志。

## 运行观察与已知限制

按 Cloudflare 官方 [Limits](https://developers.cloudflare.com/workers/platform/limits/) 和 [Pricing](https://developers.cloudflare.com/workers/platform/pricing/)，每半小时约 48 次调用属于较小规模，仍应在实际账户核实 CPU 与免费配额。

R3 修复后在 **2026-10-25** 累计约 19 小时的试运行中，报告了 39 次外部 Dispatch：**38 次成功、1 次取消**，相邻创建间隔中位数约 30 分钟，最大约 30 分 01 秒。真实生产已验证新帖翻译、SMTP 接受及 state 持久化；另有一次部分翻译失败（4 项中 2 项回退英文）和一次原因未完全明确的取消。少量 Cloudflare 日志采集缺口不能直接视作业务故障。

该观察窗口是**经用户要求提前结束**，不是原定 48 小时资格化，不能据此保证长期无故障。双 Cron 已确认保留；现阶段没有进一步切换任务。若后续需要查因，分别核对 Cloudflare `scheduled_at`／`observed_at`、GitHub `workflow_dispatch` 的创建和完成时间、SMTP 结果以及 `state.json` 变化。发生网络故障、Token 到期、持续缺槽或多次取消时，再实施针对性维护。

## 回退

当前 GitHub 原生 schedule 仍处于启用状态。如果仅需停用外部 Worker，先核查原生 Cron 正常，再将线上 Worker 设置 `ENABLED=false` 并移除 Cloudflare Cron；不要修改正式 `state.json`。如果将来原生 Cron 已被移除，则须先恢复原生 schedule，再停用外部调度。回退完成后根据实际情况撤销或轮换 Token，不得恢复旧游标。

## 离线验证

仓库根目录执行 `node --test scheduler/worker.test.mjs`，fetch 全部注入 Mock，不需要 npm 包或 Cloudflare 账户。

参考：[Cloudflare Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/)、[GitHub Workflow Dispatch 与权限](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)。
