# 云端配置

## 入口与连接

检查当前租户允许 Workflows / Teams 连接器，连接所有者拥有所需许可证。通常从 Teams 的接收 Webhook 请求模板创建流程，再在 Power Automate 编辑；复用现有流程时先检查其真实定义和用途。

Teams Webhook 触发器的身份选项必须与 GitLab 发出的请求兼容。普通 GitLab 项目 Webhook 无法自动取得 Microsoft Entra 用户令牌。若采用 Anyone + 带签名的回调 URL，该 URL 就是凭据，应保存在 GitLab Webhook 设置和私有凭据存储中。它不等同于验证 GitLab 来源；有此要求时读 [permissions.md](permissions.md)。不要把管理页 URL 误填为回调 URL。

先检查目标环境实际支持的连接器和操作。生成器中的 `teams_connection_reference` 必须替换为该流程的连接引用名称；它不是邮箱、访问令牌或完整 connection ID。生成结果只包含动作片段，不能直接作为完整流程包导入。

## 动作布局

按下表创建动作并命名，表达式引用依赖这些名称。生成的 `ui-values.json` 给出字段值，`power-automate-actions.json` 用于对照代码视图。

| 名称 | 动作 | 关键设置 |
| --- | --- | --- |
| MemberMap | Compose | 唯一 ID、用户名、邮箱映射 |
| PreviousAssignees | Select | 历史负责人投影为 `{id}` |
| Actor | Filter array / Query | 在 MemberMap 中按 actor_id 找发言者 |
| Recipients | Filter array / Query | 在 MemberMap 中筛选收件人 |
| RecipientEmails | Select | 仅保留 email |
| SendNotifications | Apply to each / Foreach | union 同一邮件数组实现去重 |
| PostCard | 循环内，Post card in a chat or channel | Flow bot；Chat with Flow bot；当前 email |
| FailureAlert | Send a message to yourself | 现有 Teams 连接所有者；运行链接和故障摘要 |
| MarkFailed | Terminate | Failed，避免告警成功掩盖通知失败 |

前六个动作顺序依赖前一动作 Succeeded。`FailureAlert` 的 runAfter 是 `SendNotifications: [Failed, TimedOut, Skipped]`，因此上游映射等失败导致循环跳过也会告警。`MarkFailed` 只保留默认的 `FailureAlert: [Succeeded]`。正常运行时告警与终止动作都应跳过；告警动作自身失败也会使运行失败。

代码视图中的 `@expression(...)` 是完整表达式。UI 表达式编辑器通常使用不含前导 `@` 的表达式；普通文本字段插值可使用 `@{expression(...)}`。直接粘贴后必须确认已识别为表达式，而不是原样文字。不要把所有字段一律套插值：数组和对象应保留类型。

Compose 在部分中文界面显示为“编辑”。先用动作类别或英文名称确认。动作面板异步加载后重新读取页面，不复用旧索引；画布视野不足时使用页面缩放或适应画布。代码视图可能只读，不要假定可以粘贴完整定义。

## GitLab 项目

确认 Maintainer / Owner 权限与当前 GitLab 版本，再配置项目 Webhook：

1. 保留 SSL 校验，填入已创建流程的签名回调 URL。
2. 选择 Issues / Work items 与 Comments / Notes 所需事件；机密事项事件仅在用户明确纳入范围时启用。
3. 根据当前 GitLab 支持情况使用 [自定义模板](../assets/gitlab-webhook-template.json.txt)。数字、数组、布尔和 null 的占位符不加引号。缺失字段不能变成字符串 `"null"`。
4. 检查实际发出的 JSON，尤其 `previous_assignees`、回复中的 `issue.author_id` / `issue.assignee_ids`、`object_attributes.action` 和事项 URL。启用 @ 提醒时，自定义模板必须包含 `"note_body": "{{object_attributes.note}}"`，否则只能通知负责人和创建者。

回复事件的 `object_attributes.id` 是评论 ID，卡片编号应使用 `issue.iid`。不同 GitLab 版本及 Work Item 类型可能使用不同事件结构；不能把 Issue 验证结果泛化为所有 Epic、Task 等类型。需要新类型时先检查真实负载，调整归一化字段和验证用例。

生成器严格匹配配置中的数字项目 ID 与项目 URL 前缀。若项目迁移、主机名变化或 URL 重定向，维护配置并重新验证，不能为了让消息通过而去掉项目过滤。

## 新回复中的个人 @

收件人规则仅在 `event_kind=note`、`action=create`、`system=false` 且 `noteable_type` 为 `Issue` 或 `WorkItem` 时读取 `note_body`。新建事项、描述修改、编辑回复和 MR 评论不会触发此分支。正文中被 @ 的已映射用户会与负责人、创建者合并，再按 Teams 邮箱去重和排除发言者的所有别名。

解析不区分用户名大小写，检查完整用户名边界，避免邮箱、相似用户名和群组路径误匹配。使用标准表达式函数 `split`、`xml` 和 `xpath`，不需要新增连接器。用户名和正文以 JSON 对象转换为 XML 数据，XPath 本身固定。先跳过常见三反引号/波浪号围栏、双反引号和单反引号代码片段，以及转义的 @。这是保守的文本规则，不是 GitLab Markdown 渲染器；复杂嵌套、任意长度围栏、缩进代码和 HTML 代码可能与 GitLab 不一致。需要完全一致的语义时另行设计渲染解析服务，不要宣称简单字符串匹配等同 GitLab 的实际提醒。

回复正文会进入现有 Power Automate 运行记录，用于收件人判断；默认卡片不包含正文。缺少 `note_body` 时按空文本处理，保留原有负责人和创建者通知。迁移现有流程时先保存并验证收件人规则，再为 GitLab 模板增加字段，保留其他卡片定制和连接。

## 验收与维护

按 [validation.md](validation.md) 先做离线路由测试，再在仅发给操作者的测试范围验证云端卡片与故障路径。入口 202 不代表 Teams 已发送成功。

后续成员变化只修改 `MemberMap`；多个 GitLab ID 可共用一个邮箱，但一个 ID 不能映射多个人。成员退出时及时移除映射；这只能缩小泄露窗口，实时校验需要额外机制。失败重试要考虑部分收件人已成功收到的情况，避免整批重复发送。

实施时核对官方文档：[GitLab Webhooks](https://docs.gitlab.com/user/project/integrations/webhooks/)、[事件负载](https://docs.gitlab.com/user/project/integrations/webhook_events/)、[Teams 连接器](https://learn.microsoft.com/en-us/connectors/teams/)、[Power Automate 错误处理](https://learn.microsoft.com/en-us/power-automate/guidance/coding-guidelines/error-handling)。
