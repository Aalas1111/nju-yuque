# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与语义化版本。

## [Unreleased]

### Added

- **目录整理 / 周切换（`yuque classroom tidy`）**：归档区永远在最下方、归档区内部最新在上、
  根目录只保留一个活跃周目录（= 今天所在那一周），周切换时自动归档上周、新建/启用本周目录。
  只移动目录节点，不改任何文档正文；默认 dry-run；也可用 `--tidy-toc` 让常驻每轮顺手整理。
- **本地产出按周分组**：通知 outbox 改成 `notify/<周>/{pending,done,outbox.jsonl}`
  （事件按自己 `created_at` 归周；旧的扁平布局下一轮自动迁移），并额外产出
  `applications/index-<周>.json` 周索引（全量 `index.json` 契约不变）。
- 申请解析更宽容：中文数字（`九月二十三日`/`三十人`/`一百二十人`）、全角数字、
  `16:10 18:00` 空格分隔、`16:10、18:00` 顿号、`下午4:00-下午6:00` 提示词重复。
- **端到端压测工具**：`scripts/mock_yuque.py`（本地仿真语雀 API：读 + 建/改/删文档 + 写操作计数）
  与 `scripts/stress_classroom.py`（真 CLI + 真 HTTP + 真 webhook 全链路压测，可打真机）。
  真机压测记录见 `docs/classroom-agent.md` §12.1。

### Fixed

- **多个时间段不再静默取第一个**：`活动时间：16:10-18:00 或 19:00-21:00`（以及自由文本里的
  「A 或者 B」）现在会**退回并说明**，而不是猜一个 —— 借错教室比让社员改一次代价大得多。
- **webhook 删除事件不再直接采信**：必须单独读一次、确实 404 才认定删除；
  读得到就忽略（不发消息、不改记录）。真机压测抓到：误信删除事件会丢本地记录，
  下一轮把同一篇文档**又重新受理一遍**，社员收到两条「已受理」。
- **删除后默认保留墓碑记录**（`deleted_at`），新增 `--purge-deleted` 才真正删记录：
  语雀回收站恢复文档 / webhook 误报删除之后，不会重复受理、重复通知或覆盖已交付的申请文件。
- `Store.counts()` 把墓碑单独统计为 `deleted`，不再混进在办申请。


### Added

- **教室申请 agent（`yuque classroom`）**：读语雀申请文档 → 判定要素 → 产出申请 JSON + 通知事件。
  子命令：`once` / `run` / `serve`（webhook + 轮询）/ `status` / `outbox` / `parse` / `schema` / `rules`。
- **webhook 接收 + 轮询兜底的常驻形态**：语雀知识库消息推送秒回 200、后台串行处理；
  轮询负责兜底与「文档被删除」的可靠发现；`--secret` 共享密钥、`--dump-webhook` 原始报文落盘。
- **本地持久化 `state.json`**：记录「哪些文档处理过、处理成什么」，草稿文档不入库；
  改动检测（`updated_at` + 正文哈希）、删除防抖（`--delete-grace`）、通知去重、墓碑（`--keep-deleted`）。
- **两套对外契约**：`applications/<id>.json`（申请，含 `docs/classroom-application.schema.json`）
  与 `notify/pending/*.json`（六种通知事件 + 渲染好的中文文案），均为文件式解耦。
- 新模块 `nju_yuque.classroom`：`rules`（纯规则）/ `contract` / `store` / `notify` / `pipeline` / `server` / `cli`。
- 文档：[`docs/classroom-agent.md`](docs/classroom-agent.md)（设计与交接）、
  [`examples/classroom_kb/`](examples/classroom_kb/)（知识库指导文档与模板源文件，改为「草稿标签」流程）。
- 142 个离线单测（规则 / 契约 / 持久化 / 通知 / pipeline / webhook / CLI，跑约 8 秒）。
- 「别给真人发错消息」的几条硬保险：删除判定三重确认（快照可信 + 连续 N 轮 + 单独读 404）、
  通知去重指纹只用稳定问题码（不受「距现在仅 47.0 小时」这类会变的文案影响）、
  通知文件名带 `notice_id`（state 重置也不会覆盖未投递事件）、
  webhook 接收端有体量上限/读写超时/密钥校验，常驻循环不会被单轮异常打死。

### Changed

- 教室申请的识别信号由「文档里的 `状态：待提交`」改为「**文档开头有没有草稿标记**」。
- 审核结论不再写回语雀：由「审批日志子文档」改为通知事件（交给 qqbot 发 QQ）。
- 节次推算新增中文数字支持（`八点到九点`、`下午两点到三点`），12 小时制歧义改为
  「放行 + WARNING」而不是退回。

### Removed

- `examples/classroom_application_agent.py`（旧状态机版）与 `tests/test_classroom_agent.py`：
  它靠改写文档状态 / 建审批日志 / 移动周目录 / 删除文档来工作，与「agent 对语雀只读」的新设计冲突，
  整体被 `nju_yuque.classroom` 取代（解析与业务规则已移植）。
- 上一版的 `--approve` / `--sync-status`（审核结果回写语雀）。

## [0.0.0] - 2026-02-21

### Added

- 双登录模式：官方令牌（`X-Auth-Token`）/ 网页 Cookie（`_yuque_session` + `yuque_ctoken`），
  后者支持弹有头浏览器登录或直接粘贴 Cookie；团队令牌自动识别所属团队。
- 只读命令：`repos` / `toc list` / `docs` / `search` / `doc get` / `table` / `members` / `watch`。
- 语雀表格（Sheet）解码：`lakesheet`（zlib 压缩 JSON）→ 二维表 / CSV / records。
- 写命令（需写权限令牌）：`doc create/update/delete`、`toc add/remove`、`repo create/delete`，
  全部支持 `--dry-run`，删除类操作强制 `--yes`。
- Cookie 模式评论：`comment list` / `comment add`（支持 @人，触发语雀通知）。
- 内置 `SKILL.md` 随包分发，`yuque skill install` 一键安装。
- 53 个离线单测（含 `httpx.MockTransport` 的请求体断言）、ruff 静态检查、GitHub Actions CI。

### Known issues

- `toc add` 之后目录读取有写后延迟，立刻 `toc list` 可能看不到新节点。
- Cookie 模式（评论/@）依赖非公开网页接口，尚未在真实登录下实测。
- 「数据表 / 多维表格」的读取能力未验证。
