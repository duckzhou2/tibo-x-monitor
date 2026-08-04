# Tibo X Monitor

每 30 分钟检查一次公开账号
[`@thsottiaux`](https://x.com/thsottiaux) 的原创帖、回复和引用帖，并通过
Gmail、QQ 邮箱或其他支持 SSL SMTP 的个人邮箱发送通知。项目使用 GitHub
Actions 运行，因此本地电脑关机后仍可工作。

## 行为

- 查询规则：`from:thsottiaux -is:retweet`
- 运行时间：每小时第 7、37 分（UTC，与时区无关）
- 首次运行：发送一封“监控已启用”邮件，只建立当前基线，不发送历史帖子
- 后续运行：将同一次检查发现的所有新内容按发布时间顺序合并为一封邮件发送
- 翻译：同一封邮件的英文内容会批量调用 DeepSeek，并在一次短重试后再降级为原文；Tibo 的内容及被回复或引用的原帖均保留原文并附上中文翻译
- 状态持久化：`state.json`
- 转发：忽略

GitHub 定时任务可能因平台负载而延迟，因此通知目标是半小时级，不是严格的
30 分钟 SLA。

## GitHub Secrets

在仓库的 **Settings → Secrets and variables → Actions** 中添加：

| Secret | 内容 |
| --- | --- |
| `X_BEARER_TOKEN` | X Developer App 的只读 Bearer Token |
| `SMTP_USERNAME` | 发件邮箱完整地址，例如 `name@gmail.com` 或 `123456@qq.com` |
| `SMTP_APP_PASSWORD` | Gmail 应用专用密码或 QQ 邮箱授权码，不是网页登录密码 |
| `ALERT_EMAIL` | 主收件邮箱 |
| `ADDITIONAL_ALERT_EMAIL` | 可选的第二个收件邮箱 |
| `DEEPSEEK_API_KEY` | DeepSeek API Key，用于英文帖子翻译 |

不要把这些值写入仓库、Issue、Actions 日志或聊天。

### Gmail

1. 为 Google 账号开启两步验证。
2. 在 Google 账号安全设置中生成一个 16 位应用专用密码。
3. `SMTP_USERNAME` 填完整 Gmail 地址，`SMTP_APP_PASSWORD` 填应用专用密码。

### QQ 邮箱

1. 登录 QQ 邮箱网页版，进入 **设置 → 账户**。
2. 开启 **POP3/SMTP 服务**并生成授权码。
3. `SMTP_USERNAME` 填完整 QQ 邮箱地址，`SMTP_APP_PASSWORD` 填授权码。

程序会根据 `@gmail.com` 或 `@qq.com` 自动选择 `smtp.gmail.com:465` 或
`smtp.qq.com:465`，并使用 SSL。其他邮箱可额外设置 `SMTP_HOST` 和
`SMTP_PORT`。

## 验证

本地测试不需要真实密钥：

```powershell
python -m unittest discover -s tests -v
```

配置三个 Secrets 后，在 GitHub 仓库的 **Actions → Monitor Tibo on X →
Run workflow** 手动运行一次。首次成功应满足：

1. 收到“监控已启用”邮件；
2. Actions 运行状态为绿色；
3. `state.json` 被 `github-actions[bot]` 更新；
4. 后续运行只通知新的帖子。
