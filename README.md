# GitLab Teams Notify Skill

通过 GitLab 项目 Webhook 和 Power Automate，把工作项分配、新回复发送到对应人员的 Teams 私聊。包含配置流程、离线生成器、收件人模拟器和发布前隐私检查。

仓库只包含通用代码与虚构示例。项目地址、成员映射、凭据、Webhook URL 和运行记录应保存在仓库外。

## 安装与使用

将 `skills/gitlab-teams-notify` 复制到 `${CODEX_HOME:-$HOME/.codex}/skills/gitlab-teams-notify`，在新的 Codex 会话中使用：

> 使用 $gitlab-teams-notify，为我指定的 GitLab 项目配置 Teams 工作项通知。

Skill 会检查现有项目和流程，确认成员的数字用户 ID，生成配置，再通过可用连接器或浏览器完成接入与验证。它不会从示例推断真实收件人，也不会自动复用另一个项目的凭据。

默认规则：新建且有负责人时通知负责人；重新分配时只通知新增负责人；新回复通知当前负责人和创建者，排除发言者并按 Teams 邮箱去重。未分配的新事项等待分配后再通知。规则可以按项目需求调整。

## 离线生成

需要 Python 3.10 或更新版本，无第三方依赖。复制 `assets/config.example.json` 到仓库外的私有目录，填写新项目配置：

```sh
python3 skills/gitlab-teams-notify/scripts/build_flow.py \
  --config /absolute/private/config.json \
  --out /absolute/private/rendered
```

生成 Power Automate **actions 片段**和 UI 字段值，不是可以直接导入的完整流程包。连接、触发器和项目 Webhook 仍需在目标环境配置。加上 `--event /absolute/private/event.json` 可离线模拟收件人，结果写入私有输出目录。

```sh
python3 -m unittest discover -s tests -v
python3 skills/gitlab-teams-notify/scripts/audit_package.py .
```

发布前可增加 `--deny-file /absolute/private/deny-terms.txt`，逐行列出本次实际账号、姓名、域名和标识；该文件也不能提交。`--staged` 检查 Git 索引中将要提交的内容。扫描只是辅助，还应审查文件清单、提交身份和历史。

## 边界

默认模板包含统一成员映射、用户 ID 比较及现有 Teams 连接的失败告警。静态映射不能证明收件人当前仍有事项访问权限；Teams 连接本身失效时，使用同一连接的告警也可能失败。运行时权限校验、独立告警和跨运行去重需要额外设计，见 skill 的参考文档。
