---
name: yuque
description: 语雀（Yuque）自动化。当用户提到「语雀 / yuque / 知识库 / 语雀文档 / 语雀表格 / 教室申请文档 / 社团填表 / 在语雀里看 / 把结果发到语雀 / 退回申请并通知 / 建知识库」等，或需要读取语雀知识库里的文档与表格、写/改文档、挂目录、做增量检测、在文档下评论 @人时，使用本 skill。通过本地 `yuque` CLI 复用登录态；读操作随时可用，写操作与评论必须用户明确授权。
---

# 语雀（Yuque）自动化

## 何时使用（触发词）

`语雀` / `yuque` / `知识库` / `语雀文档` / `语雀表格` / `社团填表` / `教室申请文档` /
`申请汇总` / `在文档下评论` / `退回意见` / `增量检测文档更新` / `建知识库` / `写文档到语雀`。

典型组合场景：**语雀填表 → 本 skill 取数 → `crb` CLI 提交教室借用申请**；
以及反向的 **`yuque doc create` 按模板生成申请文档 → `yuque toc add` 挂到父文档下**。

## 前置条件（一次性）

```bash
uv tool install "nju-yuque"          # 只读用
uv tool install "nju-yuque[login]"   # 需要浏览器登录抓 Cookie 时
yuque login --token <语雀访问令牌>    # 推荐：零浏览器；写权限由令牌 scope 决定
yuque login                          # 或：弹有头浏览器人工登录（解锁评论）
yuque doctor --json                  # 自检：模式 / 团队 / scope / 能力边界
```

- 凭证 `~/.yuque/auth.json`：**敏感文件，不要读取内容、不要上传、不要入库**。
- 团队令牌会自动识别所属团队（`doctor` 里的 `group` 以服务端为准）。
- 找不到本 skill 时：`yuque skill install --dir <harness 的 skills 目录>`。

## 两种模式与能力边界

| 能力 | `--token`（官方 OpenAPI） | Cookie（浏览器登录） |
|---|---|---|
| 列知识库 / 目录 / 文档 / 搜索 / 成员 | ✅ | 部分 |
| 读文档正文（Markdown） | ✅ | ✅ |
| **读表格 Sheet（结构化 / CSV）** | ✅ | ✅ |
| 建 / 改 / 删文档、挂目录、建知识库 | 需令牌带 `doc` / `repo` 写 scope | ✅ |
| **发评论 / 回复 / @人** | ❌（官方无评论接口） | ✅ |

> 判断写权限：看 `yuque doctor --json` 的 `capabilities.write`。
> 语雀 scope 里 `doc`（无 `:read`）= 读写；`doc:read` = 只读。
> 官方接口**没有**评论能力，也没有站内私信；要「通知某人」只能用 Cookie 模式的评论 + @，
> 或把名单汇总给负责人人工转达。

## 命令

一律加 `--json`，方便直接读结构化结果。

### 读

| 命令 | 作用 |
|---|---|
| `yuque doctor --json` | 自检：模式、团队、scope、能力 |
| `yuque repos --json` | 团队知识库列表 |
| `yuque toc list --repo <id 或 group/slug> --json` | 目录树（含父子层级） |
| `yuque docs --repo <...> -n 200 --json` | 文档列表（含 `updated_at`） |
| `yuque search <关键词> --scope group/slug --json` | 全文搜索（**建议限定知识库**） |
| `yuque doc get <链接> --json` | 读正文 Markdown（`--raw` 原样输出） |
| `yuque table <链接> --csv` | 读语雀表格 → CSV |
| `yuque table <链接> --json` | 表格 → `sheets` + `records` |
| `yuque members --json` | 成员 `user_id → 姓名` |
| `yuque watch --repo <...> --json` | 自上次以来有变动的文档（增量） |

### 写（需要写权限令牌；`--dry-run` 可先预览）

| 命令 | 作用 |
|---|---|
| `yuque doc create --repo <...> -t <标题> -f body.md [--slug] [--no-toc] [--parent <uuid>]` | 新建文档（默认自动挂目录） |
| `yuque doc update <链接> [--title] [-f body.md]` | 更新文档（只改传入字段） |
| `yuque doc delete <链接> --yes` | 删除文档（**必须显式 `--yes`**） |
| `yuque toc list / add / remove` | 目录：查看 / 挂载文档或分组 / 移除节点 |
| `yuque repo create --name <名> [--slug] [--public 2]` | 新建知识库 |
| `yuque repo delete --repo <...> --yes` | 删除知识库（**必须显式 `--yes`**） |

### 评论（Cookie 模式）

| 命令 | 作用 |
|---|---|
| `yuque comment list <链接> --json` | 列出评论 |
| `yuque comment add <链接> -m "..." --mention <login> --json` | 发评论 / @人（会触发语雀通知） |

链接既可以是完整 URL（`https://nova.yuque.com/<group>/<book>/<slug>`），
也可以是 `<group>/<book>/<slug>`；裸 slug 需要另加 `--repo`。

## 典型流程

### 1) 汇总「教室申请」文档并交给 crb

```bash
yuque repos --json                                   # 找到知识库 id / slug
yuque toc list --repo ghxd00/<book> --json           # 定位「教室申请」父节点
yuque docs --repo ghxd00/<book> -n 500 --json        # 拿全部子文档 slug
yuque doc get ghxd00/<book>/<slug> --json            # 逐篇读正文（时间/人数/教室）
```

把 `{活动名, 日期, 节次, 人数, 申请人}` 整理成 `plan.json`，再交给
`crb plan --file plan.json`（见 `crb` skill）。

### 2) 按模板批量生成申请文档（写权限）

```bash
# 先建一个分组节点，拿到 uuid
yuque toc add --repo ghxd00/<book> --type TITLE -t "2026 秋 教室申请" --json

# 为每位申请人生成一份子文档，并挂到该分组下
yuque doc create --repo ghxd00/<book> -t "张三-第3周周三 7-8节" \
  -f apply-template.md --parent <分组 uuid>

# 目录读取有写后延迟，立刻 list 可能看不到新节点，等几秒再确认
yuque toc list --repo ghxd00/<book> --json
```

### 3) 读语雀表格（每人一行）

```bash
yuque table ghxd00/<book>/<slug> --json | jq '.records'
yuque table ghxd00/<book>/<slug> --csv > applications.csv
```

`records` 已把首行当表头，形如 `[{"活动名": "...", "日期": "...", "教室": ""}]`。

### 4) 增量检测（不要用搜索代替）

```bash
yuque watch --repo ghxd00/<book> --json     # 首次记录水位线到 ~/.yuque/state.json
yuque watch --repo ghxd00/<book> --json     # 之后只返回新变动
```

### 5) 退回并通知（Cookie 模式）

```bash
yuque comment add ghxd00/<book>/<slug> \
  -m "教室与该时段课表冲突，请改到 7-8 节或换教学楼" \
  --mention <对方语雀 login> --json
```

## 硬性规则

1. **写操作必须先向用户确认目标与内容**；`doc delete` / `repo delete` / `toc remove`
   必须显式加 `--yes`，不确定时先跑 `--dry-run`。
2. **绝不打印 / 上传 / 入库 `~/.yuque/auth.json`**，输出里也不要回显令牌或 Cookie。
3. 搜索可能滞后：判断「有没有新申请」请用 `yuque docs` / `yuque watch` 比 `updated_at`。
4. 目录是**写后延迟**的：刚 `toc add` 完立刻 `toc list` 可能没有新节点，等几秒再查，
   不要据此判定失败并重试（会重复挂载）。
5. 写操作报 `需要令牌模式` 时，提示用户用写权限令牌登录，不要尝试绕过。
6. 报 `请给此 Token 添加 xxx 权限` 时，说明令牌 scope 不足。
7. 只处理「状态：待提交」的申请；`状态：草稿` 一律忽略（避免把半成品误当申请）。

## 限制

- 官方 OpenAPI 无评论接口；评论/@ 只在 Cookie 模式可用。
- Cookie 属非公开内部接口，语雀改版可能失效，登录态约 2 周过期。
- 语雀没有「表单」；填表载体只能是文档或表格（Sheet）。
- 表格（Sheet）可解；「数据表 / 多维表格」尚未验证。
- `repo delete` 会连同知识库内所有文档一起消失，务必二次确认。
