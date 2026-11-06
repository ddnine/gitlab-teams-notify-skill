---
name: gitlab-teams-notify
description: 为 GitLab 项目创建、迁移或维护 Teams 工作项私聊通知，包含 Webhook、Power Automate 收件人路由、统一成员映射、用户 ID 比较和失败告警。适用于项目通知接入与排障，不用于普通 GitLab 开发或批量发送 Teams 消息。
---

# GitLab → Teams 项目通知

实现事件驱动的项目通知，不依赖个人电脑持续轮询。先检查目标项目、现有 Webhook、Power Automate 流程和已授权连接，避免重复接入。保留用户已选的规则和已给出的授权；调用本 skill 不代表授权新增邮箱访问、长期凭据或向任意人员发送测试消息。

## 确定范围

- 从当前项目获取数字项目 ID、项目 URL 和用户数字 ID。Maintainer 通常可以管理项目 Webhook，但 Power Automate 连接、许可证和租户策略仍需分别检查。
- 使用用户提供或确认的 GitLab ID → Teams 邮箱映射。用户名仅作维护标签，不能作为稳定身份；不要从姓名相似度猜测邮箱。支持多个 ID 对应同一 Teams 账号。
- 没有既定规则时，建议：新事项通知负责人；改派只通知新增负责人；新回复通知当前负责人和创建者，排除发言者；按邮箱去重；未分配事项等待分配后通知。
- 确认事件类型、机密事项范围和故障告警对象。模板的告警动作发送给 Teams 连接所有者，不要假定它就是项目负责人。

## 配置与验证

执行云端接入时读 [references/power-automate.md](references/power-automate.md)。执行验证或排障时读 [references/validation.md](references/validation.md)。涉及访问权限、来源认证、可靠性或连接移交时读 [references/permissions.md](references/permissions.md)。

1. 将 [assets/config.example.json](assets/config.example.json) 复制到仓库外的私有目录，填写当前项目值。真实配置、签名 URL 和日志不属于 skill 资源。
2. 运行 `python3 scripts/build_flow.py --config <私有配置绝对路径> --out <私有输出目录>`。此工具离线生成 actions 片段与 UI 字段值，不创建云端流程。输出目录不能位于 skill 或代码仓库内。
3. 使用可用的服务连接器；能力不足时使用用户指定的浏览器或 Teams。先读当前页面再操作，按目标环境的连接名称配置。查看服务当前官方文档，不能假定不同版本界面、事件字段和授权方式相同。
4. 保存并检查实际动作定义、GitLab Webhook 设置和运行结果。HTTP 202 仅表示入口接受了请求；需要确认发送动作结果，必要时由收件人确认卡片可见。
5. 在私有维护记录中保存流程管理页、映射维护位置、已验证事件与剩余限制。不要把回调签名放进交付说明。

## 必须保持的逻辑

- `MemberMap` 是唯一映射来源，`Actor` 与 `Recipients` 都引用它。
- `PreviousAssignees` 先将历史负责人对象投影为 `{id}`，再比较新增 ID；用户名变更不能触发重新分配。
- 回复排除发言者时通过 ID 查其 Teams 邮箱，再排除同邮箱的所有别名；未知发言者允许走空值回退。
- 构建卡片使用 JSON 对象函数，不能把标题或操作人直接拼入 JSON 字面量。只发送必要的标题、编号、操作人和链接，不默认发送回复正文或附件。
- `FailureAlert` 只在发送循环 Failed、TimedOut 或 Skipped 时执行；`MarkFailed` 仅在告警成功后执行并把本次运行标为失败。正常发送和零收件人不能触发故障告警。
- 项目 ID 和 URL 过滤只控制路由，不证明请求来自 GitLab；静态成员表也不是实时权限校验。

## 维护与发布

新增项目重新读取该项目数据，不复用上一次的人员、项目 ID、租户或凭据。调整规则后运行离线测试，并进行范围明确的云端验证；不要重放整批生产运行来测试。

如果用户要求发布代码，发布通用 skill 和虚构示例。显式选择文件，运行 `scripts/audit_package.py <代码根目录>`，必要时用仓库外的 `--deny-file` 检查本次私有值，提交后核对作者、提交者和历史。遵循用户对个人信息的要求；不要把生产流程导出、截图、日志、真实邮箱、签名 URL 或令牌加入仓库。
