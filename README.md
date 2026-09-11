# 语雀（Yuque）自动化 CLI + Skill

> **nju-yuque** —— 一个 **CLI + Skill**：复用一次登录态，直连语雀官方 API / 网页内部接口，
> 完成 **列知识库 / 读目录 / 列文档 / 搜索 / 读正文 / 读表格 / 发评论 @人 / 增量检测**。
>
> 典型用途：**社团在语雀填表 → agent 读取并结构化 → 交给 [`crb`](https://github.com/Aalas1111/NJU_Classroom_Booking) 批量提交教室借用申请**。
>
> 命令名说明：可执行文件叫 `yuque` 而不是 `yq` —— `yq` 已被广泛用作 YAML 处理工具
> （mikefarah/yq、kislyuk/yq），装到一起会互相覆盖。

![version](https://img.shields.io/badge/version-0.0.0-orange)
![python](https://img.shields.io/badge/python-3.12%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

---

## ⚠️ 先读这段

- **默认只读**。`yuque` 的所有读取命令都不会修改语雀内容；发评论等写操作必须显式调用，
  且只在 Cookie 模式下可用。
- 凭证 `~/.yuque/auth.json` 是**敏感文件**：不要提交到 git、不要分享、不要让 AI 读取其内容。
- 本工具**不绕过任何权限**：能读到什么，取决于令牌 scope / 你自己的语雀权限。
  请遵守语雀服务协议与团队规范。

---

## 两种登录模式

| | `--token`（官方 OpenAPI） | Cookie（网页登录态） |
|---|---|---|
| 怎么登 | `yuque login --token <语雀访问令牌>` | `yuque login`（弹浏览器）或 `yuque login --cookie '<Cookie>'` |
| 原理 | 官方 `/api/v2/*` + `X-Auth-Token` | 网页内部 `/api/*` + `_yuque_session` / `yuque_ctoken` |
| 稳定性 | ✅ 官方接口，长期有效 | ⚠️ 非公开接口，登录态约 2 周过期 |
| 列表 / 搜索 / 目录 / 成员 | ✅ | 部分 |
| 读文档正文 / **读表格** | ✅ | ✅ |
| **评论 / 回复 / @人** | ❌（官方无评论接口） | ✅ |
| 写 / 改 / 删文档 | 需要带 `doc:write` 的令牌 | ✅ |

> 一句话：**能拿到带写权限的令牌就优先用令牌**；只有「发评论通知人」这件事必须走 Cookie。

---

## 环境要求

| 依赖 | 版本 | 说明 |
|---|---|---|
| Python | ≥ 3.12 | 由 `uv` 自动管理 |
| [uv](https://docs.astral.sh/uv/) | 最新 | Python 包 / 虚拟环境 / 项目管理 |
| 浏览器 | Edge 或 Chrome 任一 | 仅 `yuque login`（不带 `--token`）需要 |

---

## 安装

```bash
git clone https://github.com/Aalas1111/NJU_Yuque.git
cd NJU_Yuque
uv tool install ".[login]"     # 需要浏览器登录时装 [login]；纯令牌模式可省
yuque --version                # yuque 0.0.0
```

从源码跑：`uv sync --extra login && uv run yuque <命令>`。

---

## 快速开始

```bash
# 1) 登录（二选一）
yuque login --token <语雀团队访问令牌>     # 推荐：零浏览器
yuque login                               # 弹有头浏览器，人工登录（解锁评论）

# 2) 自检
yuque doctor --json
# {"mode":"token","host":"https://nova.yuque.com","group":"ghxd00",
#  "scopes":"group:read,repo:read,doc:read,statistic:read,private_search",
#  "capabilities":{"read":true,"write":false,"comment":false}}

# 3) 读
yuque repos --json
yuque docs  --repo ghxd00/mrge27 --json
yuque doc   https://nova.yuque.com/ghxd00/mrge27/bbf1n662v36gd85q --json
```

---

## 命令速查

| 命令 | 作用 |
|---|---|
| `yuque login [--token/--cookie] [--host/--group]` | 登录（令牌 / Cookie / 浏览器） |
| `yuque logout` | 清除本地凭证 |
| `yuque doctor --json` | 自检：模式 / 身份 / scope / 能力边界 |
| `yuque repos --json` | 知识库列表 |
| `yuque toc --repo <id 或 group/slug> --json` | 目录树（含层级） |
| `yuque docs --repo <...> -n 200 --type Sheet --json` | 文档列表（含 `updated_at`） |
| `yuque search <关键词> --scope group/slug --json` | 全文搜索 |
| `yuque doc <链接> [--raw] [--json]` | 读正文 Markdown |
| `yuque table <链接> [--csv] [--json] [--sheet <名>]` | 读语雀表格（结构化 / CSV） |
| `yuque members --json` | 成员 `user_id → 姓名` |
| `yuque watch --repo <...> [--since ISO] [--json]` | 自上次以来有变动的文档 |
| `yuque comment list <链接>` | 列出评论（Cookie 模式） |
| `yuque comment add <链接> -m "..." --mention <login>` | 发评论 / @人（Cookie 模式） |
| `yuque skill path / show / install` | 内置 AI Skill |

> 所有命令都支持 `--json`；不确定参数时加 `--help`。

### 链接写法

以下三种都可以：

```bash
yuque doc https://nova.yuque.com/ghxd00/mrge27/bbf1n662v36gd85q
yuque doc ghxd00/mrge27/bbf1n662v36gd85q
yuque doc bbf1n662v36gd85q --repo ghxd00/mrge27
yuque docs --repo 79635820          # 也可以直接给知识库数字 id
```

### 读语雀表格

语雀表格文档的正文是 `lakesheet`（zlib 压缩 JSON）。`yuque table` 会自动解码：

```bash
yuque table ghxd00/pxeuoa/xh6wvp1yb78ztg40 --json | jq '.records[:2]'
# [{"对应题号":"", "使用数据":"...", "_sheet":"Sheet1"}, ...]

yuque table ghxd00/pxeuoa/xh6wvp1yb78ztg40 --csv > data.csv
```

### 增量检测（别用搜索代替）

新发布的文档未必立刻能搜到，做「有没有新申请」判断请比 `updated_at`：

```bash
yuque watch --repo ghxd00/<book> --json     # 首次记录水位线到 ~/.yuque/state.json
yuque watch --repo ghxd00/<book> --json     # 之后只返回新变动
```

---

## 安装 AI Skill

```bash
yuque skill install --dir .pi/skills        # 装到 <dir>/yuque/SKILL.md
yuque skill show | head -20                 # 查看内容
```

`SKILL.md` 会告诉 agent：什么时候用、两种模式的能力边界、如何把语雀申请转成
`crb plan` 的输入。

---

## 配置

| 环境变量 | 作用 | 默认 |
|---|---|---|
| `YUQUE_TOKEN` | `yuque login` 未带 `--token` 时可作为令牌来源 | 无 |
| `YUQUE_HOST` | 语雀域名（团队自定义域名） | `https://nova.yuque.com` |
| `YUQUE_GROUP` | 团队 login | `ghxd00` |
| `YUQUE_HOME` | 凭证 / 状态目录 | `~/.yuque` |

---

## 开发

```bash
uv sync --dev
uv run ruff check . && uv run ruff format --check .
uv run pytest -v
uv run python scripts/sync_skill.py     # 改完 SKILL.md 后同步仓库内副本
```

---

## 免责声明

本项目是社区自发的自动化工具，与语雀官方无关。Cookie 模式依赖非公开网页接口，
语雀改版后可能失效。请自行评估风险并遵守平台规则。
