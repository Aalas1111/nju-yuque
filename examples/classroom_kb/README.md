# 教室申请知识库 · 内容源文件

| 文件 | 用途 |
|---|---|
| `guide.md` | 知识库里的《指导文档（必读）》，agent 处理规则与填表说明 |
| `template.md` | **语雀「知识库文档模板」的内容**，由老师在知识库设置里创建一次；不再单独建一篇模板文档 |

## 同步到语雀

```bash
export YUQUE_HOME=~/.yuque
REPO=lqogh0/jsjysq
uv run yuque doc update $REPO/<guide-slug> -f examples/classroom_kb/guide.md   # slug 从 yuque docs 取
```

模板：把 `template.md` 的内容贴进「知识库 → 设置 → 文档模板」即可（语雀没有对应的 OpenAPI）。

## 知识库结构（agent 维护）

```
教室借用申请
├── 指导文档（必读）        guide.md
├── <MMDD-MMDD>            每周申请目录（如 0914-0920）
│   └── <活动名称>          社员用文档模板新建，标题写活动名称
│       └── 审批日志        agent 维护
└── 归档区                  过期周目录移到这里
```

## 从零搭一个这样的知识库

```bash
uv run yuque repo create --name "教室借用申请" --slug <slug> --public 2
uv run yuque doc create --repo <repo> -t "指导文档（必读）" -f examples/classroom_kb/guide.md --no-toc
uv run yuque toc add --repo <repo> --doc-id <doc-id>
uv run yuque toc add --repo <repo> --type TITLE -t "0914-0920"
uv run yuque toc add --repo <repo> --type TITLE -t "归档区"
```

> 语雀**表格 / 数据表不能通过 API 创建**，但知识库、文档、目录都可以。
