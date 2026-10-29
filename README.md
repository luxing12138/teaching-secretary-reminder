# 教学秘书提醒器

面向 Windows 的本地任务管理与钉钉提醒工具。把通知整理成事项，在看板和月历中管理进度，在钉钉中处理提醒；每个事项保存为可编辑的 Markdown 文件，不需要启动 Obsidian。

这是一个持续完善中的个人项目，当前优先保证日常任务流转和本地数据可控。界面采用暖白底色、马卡龙配色、圆角卡片与右侧编辑区。

## 主要功能

- 四阶段看板，支持跨分类拖动、浮动卡片、落点提示、边缘滚动和 Esc 取消。
- 月历总览，点击日期新建；已完成事项不进入月历。
- 今日、逾期、搜索和紧凑的已完成列表。
- 回收站、恢复、备份、JSON 导出及批量移动和删除。
- 通知、会议、材料报送等模板，也可保存自己的模板。
- 按接收人记录已通知、已反馈进度；此记录不代表自动向这些人发送消息。
- 钉钉待办双向联动、阶段推进、单项重试及同步状态提示。
- 可选 DeepSeek 通知解析，以及供 DeepSeek Harness（DSH）等 Agent 使用的 JSON 命令接口。
- 关闭窗口收起到托盘；独立同步服务由应用启动时检查和拉起。

## 任务处理方式

默认使用完整流程：

| 看板分类 | 提醒时间 | 在钉钉完成当前待办后 |
| --- | --- | --- |
| 待传达 | 立即 | 推进为待催办 |
| 待催办 | 截止日期前 3 天 08:00 | 推进为待反馈 |
| 待反馈 | 明确的截止时间；只填写日期时默认当日 17:00 | 推进为已完成 |
| 已完成 | 不再提醒 | 不再推进 |

另有“一次性通知”类型：通知到位后，在钉钉完成一次即可结束，不再经过后续阶段。

时间按 Asia/Shanghai 解释，使用 24 小时制。钉钉每个阶段对应独立待办；同步会记录已处理的完成状态，避免同一次完成重复推进。切勿让其他自动化再次推进同一事项。

## 运行源码

环境：Windows 10/11、Python 3.11 或更新版本（包含 Tkinter）、PowerShell 7。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\pythonw.exe src/app.py
```

默认数据位置：`%USERPROFILE%\Documents\TeachingSecretaryReminder\`。

其中 `Tasks` 保存事项 Markdown，旁边的 `Trash`、`Undo`、`Audit`、`Templates`、`SyncStatus` 等目录用于回收、撤销、审计、模板及同步状态。请备份整个数据目录，不要只备份 EXE。

现有默认位置的旧 SQLite 数据仅作为迁移来源保留；当前活动数据源为 Markdown。首次启动会检查迁移，不修改 Obsidian 笔记库。

## 连接钉钉

钉钉功能依赖独立的 [DingTalk Workspace CLI（DWS）](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli)。本仓库不分发其可执行文件、运行库或登录凭据。请从该项目官方渠道安装，并按其说明完成登录及所需授权。

应用按以下顺序寻找 DWS：

1. `TSR_DWS_EXE` 环境变量指定的可执行文件。
2. 应用资源目录内的 `tools/dws.exe`。
3. 系统 PATH 中的 `dws.exe`。

```powershell
$env:TSR_DWS_EXE = 'D:\Apps\DWS\dws.exe'
.\.venv\Scripts\python.exe src/assistant_cli.py dws-status
```

本地任务管理不要求 DWS 登录。未配置或离线时，远端同步会失败并等待重试，不应据此删除本地事项。

## 自动同步与后台运行

应用检查本地文件变化，通常约 1 秒刷新界面；同步服务对本地修改做约 1 秒防抖，并默认每 15 秒轮询钉钉完成状态。钉钉完成不会直接触发本机文件事件，需先由服务取回，再写入 Markdown、刷新看板及发布下一阶段。

这些是工作节奏而非实时性承诺。电脑休眠、离线、DWS/API 延迟及服务未运行都会延后更新。关闭窗口为托盘运行，退出托盘程序与 Windows 注销不是同一件事。

如需登录后自动启动服务，可在确认路径后运行 `Install-BackgroundReminder.ps1`。脚本会创建或更新本项目自己的两个计划任务，不会操作 Obsidian 的计划任务；Windows 可能要求提升权限。安装并非运行源码的前提。

## DeepSeek 与 DSH

设置中的 API Key 仅用于将粘贴的通知解析为草稿，需确认后保存。也可设置 `DEEPSEEK_API_KEY` 环境变量。调用解析功能会把所选通知文本发送到所配置的 DeepSeek API；不需要 AI 的看板、提醒、统计和同步不会因此调用模型。

DSH 指 DeepSeek Harness，不是 DWS。推荐链路：钉钉机器人 → DSH → 本项目 CLI 建立事项 → 本项目同步服务管理后续提醒。DSH 不应同时调用旧 TaskNotes 流程处理这些事项。

详见 [Agent 接入与数据边界](docs/AGENT.md)。

## 构建 Windows 应用

```powershell
pwsh -File Build-Executable.ps1
```

输出为 `release/TeachingSecretaryReminder/TeachingSecretaryReminder.exe`。分发时保留整个目录，不要只复制 EXE。构建会收集 tzdata，避免 Windows 上找不到 Asia/Shanghai 时区。

默认不捆绑 DWS；若确需捆绑，请先核对其分发许可，并通过 `-DwsDirectory` 指定含 DWS 与其运行库的专用目录。不要把登录文件放进该目录。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe src/app.py --smoke-test "$env:TEMP\tsr-ui-smoke.json"
```

测试使用临时目录和模拟 DWS，不创建真实钉钉待办。UI 冒烟测试需要可用的 Windows 桌面。真实钉钉连通性仍需由部署者在自己的账号上验收。

## 项目结构

```text
src/markdown_store.py    Markdown 持久化与旧数据迁移
src/dws_integration.py   钉钉适配与阶段映射
src/sync_service.py      独立同步服务
src/design_ui.py         看板、月历与桌面交互
src/productivity*.py    模板、进度、撤销等扩展
src/assistant_cli.py     JSON Agent 接口
tests/                  自动化测试
```

## 使用边界

- 撤销支持部分桌面操作与接收人进度修改；并非所有 CLI 操作均可撤销。外部修改冲突时会拒绝覆盖，已发送的通知不能撤回。
- 本应用不是任意钉钉待办的全量导入工具，主要同步由自身建立或已有映射的事项。
- 请不要公开任务数据、日志中的私人内容、API Key、机器人密钥或 DWS 登录文件。
- 当前仓库未授予额外开源许可；公开可见不等于允许任意再分发。第三方依赖适用各自许可，后续可另行确定本项目许可证。
