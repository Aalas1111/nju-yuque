---
name: yuque
description: 语雀（Yuque）自动化。当用户提到「语雀 / yuque / 知识库 / 语雀文档 / 语雀表格 / 教室申请文档 / 社团填表 / 在语雀里看 / 把结果发到语雀 / 退回申请并通知」等，或需要读取语雀知识库里的文档与表格、做增量检测、在文档下评论 @人时，使用本 skill。通过本地 `yuque` CLI 复用登录态读取内容；默认只读，发评论/写操作必须用户明确授权。
---

# 语雀（Yuque）自动化

## 何时使用（触发词）

`语雀` / `yuque` / `知识库` / `语雀文档` / `语雀表格` / `社团填表` / `教室申请文档` /
`申请汇总` / `在文档下评论` / `退回意见` / `增量检测文档更新`。

典型组合场景：**语雀填表 → 本 skill 取数 → `crb` CLI 提交教室借用申请**。

## 前置条件（一次性）

```bash
uv tool install "nju-yuque"          # 只读用（官方令牌模式）
uv tool install "nju-yuque[login]"   # 需要浏览器登录抓 Cookie 时
yuque login --token <语雀访问令牌>    # 推荐：零浏览器、长期有效
yuque login                          # 或：弹有头浏览器人工登录（解锁评论/写）
yuque doctor --json                  # 自检：模式 / scope / 能力边界
```

- 凭证 `~/.yuque/auth.json`：**敏感文件，不要读取内容、不要上传、不要入库**。
- 令牌模式（`--token`）只读为主；评论/写文档只有 Cookie 模式能做。
- 找不到本 skill 时：`yuque skill install --dir <harness 的 skills 目录>`。

## 两种模式与能力边界

| 能力 | `--token`（官方 OpenAPI） | Cookie（浏览器登录） |
|---|---|---|
| 列知识库 / 目录 / 文档 / 搜索 / 成员 | ✅ | 部分（知识库列表/文档列表） |
| 读文档正文（Markdown） | ✅ | ✅ |
| **读表格 Sheet（结构化 / CSV）** | ✅ | ✅（走页面数据） |
| 发评论 / 回复 / @人 | ❌（官方无评论接口） | ✅ |
| 写 / 改 / 删文档 | 需令牌带 `doc:write` | ✅ |

> 官方接口**没有**评论能力，也没有站内私信。要「通知某人」只能用 Cookie 模式的
> 评论 + @，或把名单汇总给负责人人工转达。

## 命令

一律加 `--json`，方便直接读结构化结果。

| 命令 | 作用 |
|---|---|
| `yuque doctor --json` | 自检：模式、身份、scope、能力 |
| `yuque repos --json` | 团队知识库列表 |
| `yuque toc --repo <id 或 group/slug> --json` | 目录树（含父子层级） |
| `yuque docs --repo <id 或 group/slug> -n 200 --json` | 文档列表（含 `updated_at`） |
| `yuque search <关键词> --scope group/slug --json` | 全文搜索（**建议限定知识库**） |
| `yuque doc <链接> --json` | 读正文 Markdown（`--raw` 原样输出） |
| `yuque table <链接> --json` | 读语雀表格 → sheets + records |
| `yuque table <链接> --csv` | 表格导出 CSV |
| `yuque members --json` | 成员 `user_id → 姓名` |
| `yuque watch --repo <...> --json` | 自上次以来有变动的文档（增量） |
| `yuque comment list <链接> --json` | 列出评论（Cookie 模式） |
| `yuque comment add <链接> -m "..." --mention <login> --json` | 发评论 / @人（Cookie 模式） |

链接既可以是完整 URL（`https://nova.yuque.com/<group>/<book>/<slug>`），
也可以是 `<group>/<book>/<slug>`；裸 slug 需要另加 `--repo`。

## 典型流程

### 1) 汇总「教室申请」文档

```bash
yuque repos --json                                   # 找到知识库 id / slug
yuque toc --repo ghxd00/<book> --json                # 找到「教室申请」父节点
yuque docs --repo ghxd00/<book> -n 500 --json        # 拿全部子文档的 slug
yuque doc ghxd00/<book>/<slug> --json                # 逐篇读正文（含时间/人数/教室）
```

把读到的 `{活动名, 日期, 节次, 人数, 申请人}` 整理成 `plan.json`，
再交给 `crb plan --file plan.json`（见 `crb` skill）。

### 2) 读语雀表格（每人一行）

```bash
yuque table ghxd00/<book>/<slug> --json | jq '.records'
yuque table ghxd00/<book>/<slug> --csv > applications.csv
```

`records` 已经把首行当表头，形如 `[{"活动名": "...", "日期": "...", "教室": ""}]`。

### 3) 增量检测（不要用搜索代替）

```bash
yuque watch --repo ghxd00/<book> --json     # 首次会记录水位线到 ~/.yuque/state.json
yuque watch --repo ghxd00/<book> --json     # 之后只返回新变动
```

### 4) 退回并通知

```bash
yuque comment add ghxd00/<book>/<slug> \
  -m "教室与该时段课表冲突，请改到 7-8 节或换教学楼" \
  --mention <对方语雀 login> --json
```

## 硬性规则

1. **默认只读**。发评论、改文档前必须先向用户确认目标与内容。
2. **绝不打印 / 上传 `/ 入库` `~/.yuque/auth.json`**，也不要在输出里回显令牌或 Cookie。
3. 搜索可能滞后：做「有没有新文档」判断请用 `yuque docs/watch` 比 `updated_at`。
4. 写操作报 `需要 Cookie 模式` 时，提示用户 `yuque login`，不要尝试绕过。
5. 报 `请给此 Token 添加 xxx 权限` 时，说明令牌 scope 不足，让用户换写权限令牌或走 Cookie。
6. 只处理「状态：待提交」的申请；`状态：草稿` 一律忽略（避免把半成品误当申请）。

## 限制

- 官方 OpenAPI 无评论接口；评论/@ 只在 Cookie 模式可用。
- Cookie 属非公开内部接口，语雀改版可能失效，登录态约 2 周过期。
- 语雀没有「表单」；填表载体只能是文档或表格（Sheet）。
- 表格（Sheet）可解；「数据表 / 多维表格」尚未验证。
