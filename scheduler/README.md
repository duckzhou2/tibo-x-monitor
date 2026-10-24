# 可选外部调度（当前未部署、未启用）

唯一用途：Cloudflare Workers Cron Trigger → GitHub Workflow Dispatch → main 的既有 monitor 模式。Worker 不读取 X/DeepSeek/SMTP 凭据，不执行监控或保存游标，也没有公开 HTTP 触发入口。

wrangler.toml 的 crons=[]、ENABLED=false 是刻意保留的停用配置；R1 不改变 GitHub 原生 cron。请求超时 10 秒，接受当前 API 的 HTTP 200 及旧版 204，其他状态及网络异常明确失败，不输出响应正文。POST 不自动重试，避免接受后丢失响应引起即时重复；下个 Cron 再尝试。HTTP 接受不证明监控 job 成功或邮件送达。

## 部署前置条件（仅在 Supervisor 授权后执行）

1. R1 已审查合入 main、离线测试通过；确认 Cloudflare 账户及计划限制。
2. 人工创建有到期日的 GitHub fine-grained PAT：资源所有者选仓库所有者，Repository access **仅选 duckzhou2/tibo-x-monitor**；仅授予 **Actions: write** 和 GitHub 自动要求的 Metadata: read，不授予 Contents write。GitHub 不能进一步把此权限限定为单一 workflow，需要认可其仓库内 Actions 权限范围并定期轮换。
3. 仅在 Cloudflare 部署环境创建 Secret binding `GITHUB_DISPATCH_TOKEN`，通过安全输入注入，不写入 TOML、仓库、聊天或命令行参数。不配置任何 X/DeepSeek/SMTP Secret。
4. 用 Cloudflare 控制台或已批准的 Wrangler 安装部署本目录模块，先保持 Cron 为空、ENABLED=false；R1 不安装部署依赖。
5. 授权真实验收后设置 ENABLED=true，把 Cron 配成 `7,37 * * * *`（UTC），观察实际 dispatch、Actions、邮件和 state。配置传播可能延迟，不承诺严格半小时 SLA。
6. 外部触发验收稳定后，再单独授权移除 GitHub 原生 schedule 条目。不要禁用整个 workflow，否则 dispatch 也会失效。短期双调度会多做空轮询；全局 concurrency 和 job 开始时读取最新 main 防止同时用旧状态发送。GitHub 可能替换尚未执行的 pending job，concurrency 不是持久化事件队列。

外部调度只改善事件来源，不消除 GitHub runner 或 SMTP 故障。仍需人工定期核查 Cron 结果、Token 到期和 Actions 历史；本次不搭建独立告警平台。

## 回退

先恢复 GitHub 原生 schedule（如已移除），再设置 Worker ENABLED=false 并移除 Cron。确认原生运行恢复后按授权撤销 Token。无需改 state 或业务代码，不要删除正式状态。

## 离线验证

仓库根目录执行 `node --test scheduler/worker.test.mjs`，fetch 全部注入 Mock，不需要 npm 包或 Cloudflare 账户。

参考：[Cloudflare Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/)、[GitHub Workflow Dispatch 与权限](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)。
