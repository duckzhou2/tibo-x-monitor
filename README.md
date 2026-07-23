# Tibo X Monitor

每 30 分钟检查一次公开账号
[`@thsottiaux`](https://x.com/thsottiaux) 的原创帖、回复和引用帖，并通过
Resend 发送邮件。项目使用 GitHub Actions 运行，因此本地电脑关机后仍可工作。

## 行为

- 查询规则：`from:thsottiaux -is:retweet`
- 运行时间：每小时第 7、37 分（UTC，与时区无关）
- 首次运行：发送一封“监控已启用”邮件，只建立当前基线，不发送历史帖子
- 后续运行：按发布时间顺序逐条发送新内容
- 状态持久化：`state.json`
- 转发：忽略

GitHub 定时任务可能因平台负载而延迟，因此通知目标是半小时级，不是严格的
30 分钟 SLA。

## GitHub Secrets

在仓库的 **Settings → Secrets and variables → Actions** 中添加：

| Secret | 内容 |
| --- | --- |
| `X_BEARER_TOKEN` | X Developer App 的只读 Bearer Token |
| `RESEND_API_KEY` | Resend API Key |
| `ALERT_EMAIL` | 接收通知的邮箱 |

不要把这些值写入仓库、Issue、Actions 日志或聊天。

默认发件人为 `Tibo Monitor <onboarding@resend.dev>`。Resend 的测试域名只能发给
Resend 注册账号所使用的邮箱；若要发给其他地址，需要在 Resend 验证自己的域名，
并通过 `ALERT_FROM` 环境变量指定发件人。

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
