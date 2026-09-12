# 教室申请知识库 · 内容源文件

这两个文件是**知识库内实际文档的唯一事实来源**，改完请同步推送到语雀：

| 文件 | 对应知识库文档 |
|---|---|
| `guide.md` | `00-指导文档（必读）` |
| `template.md` | `教室申请模板（复制后填写）` |

同步命令（写权限令牌）：

```bash
export YUQUE_HOME=~/.yuque
REPO=lqogh0/jsjysq
uv run yuque doc update $REPO/<guide-slug>    -f examples/classroom_kb/guide.md
uv run yuque doc update $REPO/<template-slug> -f examples/classroom_kb/template.md
# slug 从 `uv run yuque docs --repo $REPO --json` 里取
```

知识库结构（agent 维护）：

```
教室借用申请
├── 00-指导文档（必读）        guide.md
├── 教室申请模板（复制后填写）  template.md
├── <MMDD-MMDD>               每周申请目录（如 0914-0920）
└── 归档区                    过期周目录移到这里
```

> ⚠️ 语雀表格/数据表**不能**通过 API 创建，知识库和目录可以用
> `yuque repo create` / `yuque toc add` 自动搭出来，指导文档与模板用 `yuque doc create -f`。
