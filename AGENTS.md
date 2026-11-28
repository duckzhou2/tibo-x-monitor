# AI X Monitor

- 用中文沟通。账号及所属平台由 `accounts.json` 管理。生产仅监控 Tibo Sottiaux 和 Varun Mohan。
- GitHub Actions 的 `monitor.yml` 是唯一业务入口。Cloudflare Worker 以固定仓库 ID 调用它，两条 Cron 均为 `7,37 * * * *`（UTC）。
- `state.json` 是正式状态。Tibo 沿用顶层字段，其余账号状态按用户名保存。维护时以最新 main 为准，保留游标和历史，使用临时状态做离线测试。
- 修改可用 `python -B -m unittest discover -s tests` 和 `node --test scheduler/worker.test.mjs` 验证。
- Cloudflare 部署、凭据和回退见 `scheduler/README.md`。仓库中的 Wrangler 配置是停用模板，正式部署前核对线上 Cron 与 Secret 绑定。
