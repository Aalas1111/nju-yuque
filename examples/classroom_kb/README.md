# 教室申请知识库 · 内容源文件

| 文件 | 用途 |
|---|---|
| `guide.md` | 知识库里的《指导文档（必读）》，agent 处理规则与填表说明 |
| `template.md` | **语雀「知识库文档模板」的内容**，由老师在知识库设置里创建一次；不再单独建一篇模板文档 |

> 识别规则只有一条：**文档开头（前 3 个非空行）出现「草稿」二字 → 不处理**。
> 所以模板第一行是 `【草稿】…`，社员填完把它删掉，agent 才会接管。

## 同步到语雀

```bash
export YUQUE_HOME=~/.yuque
REPO=lqogh0/jsjysq
uv run yuque doc update $REPO/<guide-slug> -f examples/classroom_kb/guide.md   # slug 从 yuque docs 取
```

模板：把 `template.md` 的内容贴进「知识库 → 设置 → 文档模板」即可（语雀没有对应的 OpenAPI）。

## 知识库结构（人工维护，agent 只读）

```
教室借用申请
├── 指导文档（必读）        guide.md    ← agent 按 doc_id 锁定，永不处理
├── <MMDD-MMDD>            每周申请目录（如 0914-0920）
│   └── <活动名称>          社员用文档模板新建，标题写活动名称
└── 归档区                  过期周目录可以手工移进来，agent 一律不碰
```

agent **不再**创建 `审批日志`、**不再**移动目录、**不再**改文档状态。
归档、目录整理都由人工做（也可以后续用 `yuque toc` 命令脚本化）。

## 从零搭一个这样的知识库

```bash
uv run yuque repo create --name "教室借用申请" --slug <slug> --public 2
uv run yuque doc create --repo <repo> -t "指导文档（必读）" -f examples/classroom_kb/guide.md --no-toc
uv run yuque toc add --repo <repo> --doc-id <doc-id>
uv run yuque toc add --repo <repo> --type TITLE -t "0914-0920"
uv run yuque toc add --repo <repo> --type TITLE -t "归档区"
```

## 让 agent 跑起来

```bash
# 只读令牌即可
uv run yuque login --token <令牌>

# 先看一眼（不落盘、不发通知）
uv run yuque classroom once --repo <repo> --dry-run

# 正式跑一轮
uv run yuque classroom once --repo <repo>

# 常驻：webhook + 轮询兜底
uv run yuque classroom serve --repo <repo> --port 8765 --secret <自定义密钥>
```

产出目录（默认 `~/.yuque/classroom/<repo>/`）：

```
applications/*.json     要素齐备的申请（交给负责提交的同学）
applications/index.json 申请索引
notify/pending/*.json   待投递的通知事件（交给 qqbot）
notify/outbox.jsonl     只追加的通知审计流水
state.json              「哪些文档处理过了」——agent 自己的记忆
```

详见 [`../../docs/classroom-agent.md`](../../docs/classroom-agent.md)。

> 语雀**表格 / 数据表不能通过 API 创建**，但知识库、文档、目录都可以。
