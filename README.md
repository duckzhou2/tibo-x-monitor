# Tibo X Monitor

个人 X 账号 `@thsottiaux` 邮件监控：查询原创、回复和引用，排除转发；保留完整英文原文，尽量附上简体中文翻译，同批新帖合并为一封邮件。业务只在 GitHub Actions 中运行。

## 调度与当前边界

现有 GitHub cron 保留为 `7,37 * * * *`（UTC），计划每半小时检查。**这不是实际半小时保证。**R0 的 2026-10-10—10-24 统计窗口中，理论 672 次、实际 75 次，中位间隔约 4 小时 33 分钟。

`scheduler/` 提供尚未启用的 Cloudflare Cron → GitHub workflow_dispatch 组件，只调用现有 monitor 模式。当前没有 Cron 且 ENABLED=false。本次不部署、不创建 Token、不关闭原生 cron，见 [调度说明](scheduler/README.md)。

全局 concurrency 覆盖 schedule 和手动模式。Job 获得执行机会后重新 fetch 最新 main，读取最新正式状态，避免使用事件创建时的旧游标。正式 job 仅允许 main 执行。

## 抓取与覆盖范围

查询为 `from:thsottiaux -is:retweet`，显式按最新顺序分页。校验结果数量、正文、ID、分页顺序及错误；按 ID 去重，重复 token、分页停滞或主结果完整性不明时失败，不推进游标。只有明确指向已请求引用帖或用户的不可用错误可以降级；邮件标明缺失的引用正文。长帖优先用 note_tweet.text，上下文只包括一层回复或引用。

401/403 直接失败；429、5xx、暂时网络故障最多三次请求。遵守 Retry-After（秒数或 HTTP 日期）和限流重置时间；等待超过 20 秒或剩余预算时停止，不提前违反等待约束。请求共享约 150 秒业务预算，SMTP socket 超时 30 秒，Git 持久化预算 45 秒；Actions 5 分钟硬超时是最终边界。

**Recent Search 仅覆盖最近 7 天。**超过窗口的失联无法保证补齐，resend 也不能保证区间完整。成功检查时间未知或超过 7 天时显示 COVERAGE_WARNING，需人工核查，不代表已证明漏帖。搜索索引延迟、删除或 API 未报告的遗漏也无法仅靠 ID 游标完全消除。

## 翻译与英文回退

DeepSeek 对主帖及可用上下文按去重文本分批翻译：每批最多 12 项、合计 6000 字符，每批最多两次尝试；第二次仅重试未完成项，保留成功译文。单项超过字符上限时完整保留原文并明确回退，不截断帖子。单次请求超时最多 15 秒，仍受共享预算约束。

使用文本 SHA-256 来源 ID 和原文回显映射，顺序变化不会错配。拒绝空串、null、非字符串、缺项、重复 ID、未知来源、原文不匹配和明显未译结果，并检查 URL、@mention、标签及行内代码是否保留。结构校验不能证明模型语义翻译准确。

翻译失败**不阻止通知**：成功项保留中文，失败项标注“中文翻译失败，已保留英文原文”；Key 缺失时标注“翻译服务未配置”。日志及 Step Summary 显示待译、成功、失败数量和 DEGRADED / TRANSLATION_FALLBACK；单纯翻译降级不会使 workflow 失败。

monitor、sample、resend 共用此逻辑，不扫描邮件文字判断成功。SMTP 全部接受后，即使英文回退也推进游标，下轮不会专门重发缺译帖子。需要重新翻译时可手动 resend，没有待补译队列。

## SMTP 与正式状态

**state.json 是正式业务状态，必须保留。**兼容当前两字段格式，成功检查后增加 last_checked_at。有新帖时保存游标和检查时间，无新帖时至多每天更新一次检查时间，避免每半小时生成心跳提交。

- 全部配置收件人被 SMTP 接受才算通知成功。部分拒收、全部拒收、发送异常均失败，不推进游标；不输出收件人地址或服务器敏感报错正文。
- SMTP 接受不等于最终送达。采用至少一次尝试投递语义，允许故障重试时少量重复；Message-ID 帮助识别批次，不保证服务器去重。
- 严格校验状态类型、ID、初始化标志和时间，禁止游标回退。同目录临时文件经 flush/fsync 后原子替换。
- 状态意外缺失或损坏时停止，**不会自动初始化跳过历史**。Supervisor 应先确定最后可信状态，不要从旧本地副本覆盖 main。
- 首次部署须显式运行 `python -B monitor.py --state <新部署状态路径> --initialize`，只发启用通知、建立基线。已初始化状态拒绝再次初始化，当前生产仓库不需要此操作。

业务 step 成功后才运行 persist_state.py。它比较启动快照、候选状态和最新远端状态，在最新远端树上生成只修改 state 的提交，保留其他并发代码修改。普通 push 最多尝试三次，每次重新 fetch 并比较 state；不 force push、不盲目取最大 ID、不 reset 候选状态。远端已保存完全相同状态时明确报告 ALREADY_PERSISTED_IDENTICAL_STATE；其他远端状态变化则失败，等待核对。

**SMTP 接受后、state 成功 push 前仍有重复窗口。**中断、磁盘故障、push 失败或 DATA/QUIT 后断线都可能造成下轮重复。摘要保留批次标识、接受／拒收数量和持久化结果；不能承诺严格恰好一次。失败后先核查这些信息，再决定重试或补发。

## GitHub 配置

在仓库 Actions Secrets 中配置，禁止将值写入仓库、Issue、日志或聊天：

| 配置 | 用途 |
|---|---|
| X_BEARER_TOKEN | X API 读取凭据 |
| SMTP_USERNAME | 发件邮箱 |
| SMTP_APP_PASSWORD | 应用专用密码或邮箱授权码 |
| ALERT_EMAIL | 主收件人 |
| ADDITIONAL_ALERT_EMAIL | 可选第二收件人 |
| THIRD_ALERT_EMAIL | 可选第三收件人，也是 test-email 目标 |
| DEEPSEEK_API_KEY | 可选；缺失时明确英文回退 |

Gmail/Googlemail、QQ 自动选择 SSL SMTP 主机，默认端口 465。其他服务商在 repository Variables 配置 SMTP_HOST、SMTP_PORT；workflow 兼容同名 Secrets，Variables 优先。Gmail 使用应用专用密码，QQ 使用 SMTP 授权码。

生产 job 使用 contents: write 保存 state；离线 CI 只有 contents: read，不传生产 Secrets。手动输入经环境变量传递，不插入 shell 源码；帖子 ID 必须为 1—19 位正整数字符串，resend 下界必须小于上界。

## 手动模式

在 main 的 Actions → Monitor Tibo on X → Run workflow 选择：

| 模式 | 行为 | 修改正式 state |
|---|---|---|
| monitor | 正常检查和通知 | 按上述规则 |
| sample | 最近一条帖子示例，可带英文回退 | 否 |
| resend | resend_since_id 为不含下界，resend_through_id 为包含上界；仅最近 7 天可检索内容 | 否 |
| test-email | 仅向第三收件人发测试，不调用 X/DeepSeek | 否 |

这些模式会真实调用相应服务。R1 离线测试不会触发它们。非法输入在调用服务前拒绝。

## 离线测试和 CI

仅需 Python 3.13、Node.js 24，没有运行时第三方依赖：

```powershell
python -B -m unittest discover -s tests -v
node --test scheduler/worker.test.mjs
```

服务全部使用 Mock；Git 冲突测试使用临时本地 bare 仓库，状态测试使用临时 fixture。禁止拿正式 state 做实验。

Offline tests 在相关 Python、workflow 或 scheduler PR 变更和 main 对应 push 时运行；仅更新 state 不触发测试。CI 不执行生产监控、不传生产 Secrets。分支保护与外部调度启用留待 Supervisor 验收。

## 生产验收

先审查综合 Draft PR 与 CI，确认无生产游标 diff，再授权合并。后续分别验证无新帖、正常中文、英文回退、手动模式、SMTP 全部接受和 state push。真实服务测试、Secret 调整和外部调度切换均需后续授权。旧分支及 Draft PR #1 留到 R2。
