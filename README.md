# AI X Monitor

AI X Monitor 监控以下两个 X 账号。原创、回复和引用帖会进入通知，转发会被排除。同一轮的新帖按帖子 ID 从旧到新合并为一封邮件，每条都标明作者、所属平台、发布时间、原文、中文翻译和原帖链接。无新帖时静默完成。业务在 GitHub Actions 中运行。

| 平台 | 人员 | X 账号 |
|---|---|---|
| OpenAI | Tibo Sottiaux | `@thsottiaux` |
| Google Antigravity | Varun Mohan | `@_mohansolo` |

账号由 [accounts.json](accounts.json) 配置。新增账号只需添加用户名、显示名和平台。首次成功查询采用单页最多 10 条且省略上下文扩展，只以最新帖子 ID 建立基线。七天窗口内为空时保存首次查询时间作为下限，避免之后索引到的历史帖进入通知。

## 调度与当前边界

**当前生产为双调度**：Cloudflare Workers Cron 每半小时触发 GitHub `workflow_dispatch`（`monitor` 模式）；GitHub 原生 `schedule` 同时保留作为冗余。两边均配置 `7,37 * * * *`（UTC）。X 查询、翻译、邮件和游标更新仍只在 GitHub Actions 中执行。

R0 的 2026-10-10—24 统计窗口里，GitHub 原生 Cron 理论 672 次、实际 75 次，中位间隔约 4 小时 33 分钟，因此引入 Cloudflare。2026-10-25 的 R3 并行试运行约 19 小时，外部 Dispatch 记录 38 次成功、1 次取消，间隔中位数 30 分钟，最大约 30 分 01 秒。用户已批准按实际观察提前收口，**不宣称完成原计划的 48 小时验证**。一次取消的具体原因未确认，但未发现由此造成的游标回退或通知中断。当前双 Cron 是刻意保留的正式运行配置。

**注意：**仓库中的 `scheduler/wrangler.toml` 是安全停用模板（`ENABLED=false`、`crons=[]`），**不代表 Cloudflare 已部署的线上设置**；重新部署时必须先核对实际配置，避免误停现有调度。详情见 [调度维护说明](scheduler/README.md)。

全局 concurrency 覆盖 schedule 和手动模式。Job 获得执行机会后重新 fetch 最新 main，读取最新正式状态，避免使用事件创建时的旧游标。正式 job 仅允许 main 执行。

## 抓取与覆盖范围

按账号查询 `from:<username> -is:retweet`，显式按最新顺序分页。校验结果数量、正文、ID、分页顺序及错误；按账号和帖子 ID 去重。重复 token、分页停滞或主结果完整性不明时保留该账号原游标。只有明确指向已请求引用帖或用户的不可用错误可以降级，邮件标明缺失的引用正文。长帖优先用 note_tweet.text，上下文只包括一层回复或引用。

401/403 直接失败；429、5xx、暂时网络故障最多三次请求。遵守 Retry-After（秒数或 HTTP 日期）和限流重置时间；等待超过 20 秒或剩余预算时停止，不提前违反等待约束。多账号抓取平分 110 秒的上限，为后续账号和翻译留出时间。请求共享约 150 秒业务预算，SMTP socket 超时 30 秒，Git 持久化预算 45 秒，Actions 总上限为 5 分钟。

**Recent Search 仅覆盖最近 7 天。**超过窗口的失联无法保证补齐，resend 也不能保证区间完整。成功检查时间未知或超过 7 天时显示 COVERAGE_WARNING，需人工核查，不代表已证明漏帖。搜索索引延迟、删除或 API 未报告的遗漏也无法仅靠 ID 游标完全消除。

## 翻译与英文回退

使用 `deepseek-flash`，通过 Chat Completions 显式设置 `thinking: {"type":"disabled"}` 和 `response_format: {"type":"json_object"}`，避免日常翻译启用思考模式。参考：[模型与版本](https://api-docs.deepseek.com/quick_start/pricing/)、[非思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)。

对主帖及可用上下文按去重文本分批翻译：每批最多 12 项、合计 6000 字符，每批最多两次尝试；第二次仅重试未完成项，保留成功译文。单项超过字符上限时完整保留原文并明确回退，不截断帖子。保留此批次上限以限制原文回显、译文和 JSON 的输出体积，显式设置 `max_tokens=16384` 留出余量，单次超时从 15 秒调整为 30 秒，仍受共享 150 秒业务预算和 5 分钟 Job 上限约束。这些是保守工程预算，未通过付费请求验证延迟，不保证所有文本均能在预算内翻译。

只有 `finish_reason="stop"` 的响应进入逐项校验；`length` 等异常结束、缺失结束原因、空或无效 JSON 会有限重试，仍失败则明确英文回退。即使正文恰好能解析为完整 JSON，也不把 `length` 标记的结果当作完整译文。字符数不等于 Token 数，较长文本仍可能截断或超时；完整英文通知不受影响。参考：[JSON Output](https://api-docs.deepseek.com/guides/json_mode/)、[Chat Completions 参数和结束原因](https://api-docs.deepseek.com/api/create-chat-completion/)。

使用文本 SHA-256 来源 ID 和原文回显映射，顺序变化不会错配。拒绝空串、null、非字符串、缺项、重复 ID、未知来源、原文不匹配和明显未译结果，并检查 URL、@mention、标签及行内代码是否保留。结构校验不能证明模型语义翻译准确。

原文回显会增加输出 Token，但保留了独立的来源核对，本项目优先避免错配，不为微小成本删除此检查。URL 句末标点不作为链接身份的一部分，括号属于 URL 时仍保留。已经是中文、夹带 API/GPU 等术语的正文，模型返回不变的中文可以是合法结果，不再仅因与原文相同就误标失败；原样返回纯英文仍被拒绝。认证失败会停止后续翻译批次，剩余内容明确英文回退。

翻译失败**不阻止通知**：成功项保留中文，失败项标注“中文翻译失败，已保留英文原文”；Key 缺失时标注“翻译服务未配置”。日志及 Step Summary 显示待译、成功、失败数量和 DEGRADED / TRANSLATION_FALLBACK；单纯翻译降级不会使 workflow 失败。

monitor、sample、resend 共用此逻辑，不扫描邮件文字判断成功。SMTP 全部接受后，即使英文回退也推进游标，下轮不会专门重发缺译帖子。需要重新翻译时可手动 resend，没有待补译队列。

## SMTP 与正式状态

**state.json 是正式业务状态，必须保留。**Tibo 沿用现有顶层 `initialized`、`last_seen_id` 和 `last_checked_at`。其他账号按用户名保存在 `accounts` 下，拥有独立游标与成功检查时间。有新帖时保存对应游标和检查时间，无新帖时每个账号至多每天更新一次检查时间。

- 全部配置收件人被 SMTP 接受才算通知成功。部分拒收、全部拒收、发送异常均失败，不推进游标；不输出收件人地址或服务器敏感报错正文。
- SMTP 接受不等于最终送达。采用至少一次尝试投递语义，允许故障重试时少量重复；Message-ID 帮助识别批次，不保证服务器去重。
- 严格校验状态类型、ID、初始化标志和时间，禁止游标回退。同目录临时文件经 flush/fsync 后原子替换。
- 状态意外缺失或损坏时停止，**不会自动初始化跳过历史**。Supervisor 应先确定最后可信状态，不要从旧本地副本覆盖 main。
- 全新部署须显式运行 `python -B monitor.py --state <新部署状态路径> --initialize`，为历史主账号建立初始状态并发启用通知。现有生产直接沿用状态。新增的配置账号在正常监控轮静默建立基线。

单个账号抓取失败时，健康账号继续通知并写入候选状态。Actions 先持久化健康账号，再标记该轮部分失败。共享 SMTP 失败时保留已发现帖子的原游标，供下轮重试。persist_state.py 比较启动快照、候选状态和远端状态，在最新远端树上生成只修改 state 的提交。普通 push 最多尝试三次，每次重新 fetch 并比较 state。远端已保存完全相同状态时报告 ALREADY_PERSISTED_IDENTICAL_STATE，其他远端状态变化需要人工核对。

上线前确认 Actions 仍可向 main 提交正式 state；如果以后启用强制 PR 或 required checks 的分支保护，应先设计并验证状态提交权限，不能直接套用会阻断机器人 push 的规则。纯 state 提交不运行完整 CI，不能依赖它产生 required check。

**SMTP 接受后、state 成功 push 前仍有重复窗口。**中断、磁盘故障、push 失败或 DATA/QUIT 后断线都可能造成下轮重复。摘要保留批次标识、接受／拒收数量和持久化结果；不能承诺严格恰好一次。失败后先核查这些信息，再决定重试或补发。

摘要或诊断文件不可写时记录 `OBSERVABILITY_WRITE_FAILED`，不改变已完成的业务结果、不阻止 state push；业务失败也不会因此变成成功。邮件保留 Date 和稳定批次 Message-ID，HTML 对正文转义并保留代码缩进。

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

在 main 的 Actions → AI X Monitor → Run workflow 选择。sample 和 resend 通过 `account` 选择已配置账号，默认 `thsottiaux`。

| 模式 | 行为 | 修改正式 state |
|---|---|---|
| monitor | 正常检查和通知 | 按上述规则 |
| sample | 最近一条帖子示例，可带英文回退 | 否 |
| resend | resend_since_id 为不含下界，resend_through_id 为包含上界；仅最近 7 天可检索内容 | 否 |
| test-email | 仅向第三收件人发测试，不调用 X/DeepSeek | 否 |

这些模式会真实调用相应服务；离线测试不会触发生产服务。非法输入在调用服务前拒绝。

## 请求量与服务成本

每轮基础 Recent Search 请求从 1 次增至 2 次，分页与临时错误重试另计。Cloudflare 目标为每天 48 轮，即每天 96 次基础请求，加上 GitHub 原生实际执行的轮数乘以 2。两条调度频率与 Actions job 数量保持原配置，Runner 时长增加一次查询及有新帖时的翻译时间。

X 按返回资源计费。按公开费率，普通帖子读取为 $0.005/项、用户读取为 $0.010/项，UTC 日内重复资源通常只计一次。Varun 首次查询最多 10 条且省略扩展，按普通帖子费率至多 $0.05。日常新增费用随实际新帖、上下文和返回用户数变化。DeepSeek 只处理有新帖时需要翻译的正文和上下文，计费随 Token 数变化。参见 [X API 定价](https://docs.x.com/x-api/getting-started/pricing) 和 [DeepSeek 模型定价](https://api-docs.deepseek.com/quick_start/pricing/)。

## 离线测试和 CI

仅需 Python 3.13、Node.js 24，没有运行时第三方依赖：

```powershell
python -B -m unittest discover -s tests -v
node --test scheduler/worker.test.mjs
```

服务全部使用 Mock；Git 冲突测试使用临时本地 bare 仓库，状态测试使用临时 fixture。禁止拿正式 state 做实验。

Offline tests 在相关 Python、workflow 或 scheduler 代码变更和 main 对应 push 时运行；仅更新 state 不触发测试。CI 不执行生产监控、不传生产 Secrets。若将来启用 main 分支保护，须先验证不会阻断 Actions 持久化 state。

## 生产验收与长期维护

**2026-10-25 单账号生产验收记录。**可靠性升级 [PR #8](https://github.com/duckzhou2/ai-x-monitor/pull/8) 与 Cloudflare 兼容性修复 [PR #9](https://github.com/duckzhou2/ai-x-monitor/pull/9) 已普通合并。该次验收通过 Python 115 项和 Worker 18 项离线测试。

真实运行已验证：无新帖正常退出；原创和回复／引用上下文抓取；DeepSeek 成功翻译及部分失败的英文回退；SMTP 全部收件人接受；`state.json` 正常推进及持久化；Cloudflare 自然 Cron 成功创建并执行 GitHub Workflow。**SMTP 接受不是收件箱送达的绝对保证；异常退出仍可能造成少量重复邮件。**

2026-10-25 的约 19 小时并行观察经用户同意提前收口，保留 Cloudflare + GitHub 原生双 Cron；不安排额外 48 小时验收，也不删除其中一个触发器。独立自动巡检任务已按用户要求停止，**监控本身仍正常运行**。

维护时可查 [GitHub Actions](https://github.com/duckzhou2/ai-x-monitor/actions)、Cloudflare 定时任务和正式游标。GitHub Fine-grained PAT 到期前需要续期。任何更新均以最新 `origin/main` 和最新 `state.json` 为准。需要暂时回退到仅监控 Tibo 时，移除账号配置里的 Varun 条目即可，保留他的状态和最新 Tibo 游标。恢复配置时会沿用原游标。部署与回退见 [scheduler/README.md](scheduler/README.md)。

端到端离线演练可单独复核：`python -B -m unittest discover -s tests -p test_qualification.py -v`；无需真实凭据或生产调用。
