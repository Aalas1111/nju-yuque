# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与语义化版本。

## [Unreleased]

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
