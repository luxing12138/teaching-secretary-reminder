# DeepSeek Harness / Agent 接入说明

目标：接收用户转发的通知，在独立提醒器中建立事项。Obsidian、TaskNotes 及旧自动化均不在新链路中。后续钉钉发布和完成回写由提醒器负责；不要另外建立重复的钉钉提醒。

## 调用

在仓库根目录调用 `.venv/Scripts/python.exe src/assistant_cli.py`。程序在 stdout 返回 JSON；错误写 stderr 并返回非零退出码。保留中文编码，可使用 `-X utf8`。所有 ID 均从实际返回读取，不要自行推测。

```powershell
.\.venv\Scripts\python.exe -X utf8 src/assistant_cli.py health
.\.venv\Scripts\python.exe -X utf8 src/assistant_cli.py list --include-done
```

测试必须使用隔离目录，`--db` 为兼容参数，实际指定 Markdown 目录，必须放在子命令之前：

```powershell
.\.venv\Scripts\python.exe -X utf8 src/assistant_cli.py --db "$env:TEMP\tsr-agent-test\Tasks" create --title "示例：培训心得报送" --due "2030-10-31T10:00" --workflow-mode staged --reject-duplicate
```

以上为虚构示例；不要把年份、任务内容用于真实用户通知。

## 建立事项的规则

1. 提取通知来源、标题、要求、对象、部门和截止时间。保留原文到 source-notice/details，来源有稳定 ID 时传 source-id。
2. 时间使用 Asia/Shanghai、24 小时制。明确几点就保留；仅有日期时默认 17:00。年份不明确且会改变含义时询问，不凭示例年份猜测。
3. 默认 `staged` 完整流程。只有用户明确是一次性通知、无需反馈时选 `notice`。
4. 先查 `duplicates`，再 `create --reject-duplicate`。同一通知重试不可重复建任务。
5. 本地保存成功后，需要真实提醒时使用 `--publish` 或 `publish-dws ID`。发布失败不表示本地创建失败；读取返回后重试同步已有 ID，不再 create。

```text
duplicates --title TITLE --due DATE --source-id SOURCE_ID
create --title TITLE --due DATE [--details TEXT] [--recipients A,B]
       [--department NAME] [--category NAME] [--source-notice TEXT]
       [--source-id ID] [--semester TEXT] [--workflow-mode staged|notice]
       [--reject-duplicate] [--publish]
get ID
publish-dws ID
sync-task ID
sync-dws
dws-status
```

## 状态和扩展能力

- `open` 待传达 → `notified` 待催办 → `followed-up` 待反馈 → `done` 已完成。
- `notice` 任务从待传达直接完成。不要改为中间状态。
- 钉钉完成后运行同步读取状态，不要额外 `advance`，否则会多推进一次。`advance` 仅为旧兼容命令，不建议用于已关联任务。
- 用户明确要求人工移动时才用 `move ID --status STATUS`，随后同步已有事项。
- `progress ID --recipient NAME --notified yes|no --feedback yes|no` 记录接收人进度，不发送消息、不自动推进总状态。
- `focus today|overdue`、`templates`、`trash`、`report daily|weekly` 用于查询。
- `delete ID --confirm` 仅在用户明确要求删除时使用；可 `restore ID`。不要清空回收站。
- `undo` 仅撤销支持的操作，并可能因外部修改冲突而拒绝；不要承诺任何操作都能撤销。
- 模板保存、现有事项完整编辑和批量交互使用桌面应用。CLI 没有通用 update/batch 命令，不要虚构接口。

## 数据与运行边界

默认读取用户 Documents 下 TeachingSecretaryReminder/Tasks。不要把测试写入默认目录，也不要同时让旧 Obsidian 同步脚本处理该目录。需要修改 Markdown 时优先使用 CLI；保留任务 ID、阶段映射和不认识的字段，避免覆盖同步正在更新的文件。

DSH 与提醒器无常驻依赖关系：DSH 负责理解与建项，提醒器服务负责后台同步。钉钉完成状态依赖服务默认每 15 秒轮询，并非 1 秒实时回调。检测文件变动约 1 秒刷新不代表钉钉端也能 1 秒完成。

API Key、DWS 凭据和机器人配置不进入提示词、Git 或导出。Agent 不应修改系统环境、关闭旧工作流、清空数据或发布真实测试通知，除非用户明确要求。
