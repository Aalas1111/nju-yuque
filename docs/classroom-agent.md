# 教室借用申请 agent · 设计与交接文档

> 面向三位同学（李赫 / 谷和平 / 王恩成）与 CAC。读完这份文档，你应该能：
> ① 知道这个 agent 到底做什么、**不**做什么；② 知道它产出什么、你该消费什么；
> ③ 知道怎么把它跑起来、出问题先看哪里。

---

## 0. 一句话

**读社员在语雀里填的申请文档 → 把「确定要素」抽出来 → 要素齐备就打包成固定格式 JSON，
要素不对 / 文档被改 / 文档被删就用通知事件告诉社员。**

三条铁律：

1. **对语雀只读**：不改正文、不写状态、不建审批日志、不移动目录、不删文档。
2. **状态只在本地**：`state.json` 记录「哪篇文档处理过、处理成什么」。
3. **唯一的输入信号是草稿标签**：文档开头有「草稿」→ 不处理；删掉草稿 → agent 接管。

```
                 ┌─────────────────────── 语雀知识库（人写、agent 只读）───────────────────────┐
                 │  指导文档（按 id 锁定）      0914-0920/<活动名称>          归档区（永不处理）  │
                 └──────────────────────────────────┬──────────────────────────────────────┘
                       webhook（实时，需公网）        │        轮询（兜底，默认 60s）
                                                    ▼
   ┌────────────────────────────── nju_yuque.classroom.pipeline ──────────────────────────────┐
   │  ① 拉目录 + 文档列表  ② 比对 state.json（updated_at / 正文哈希）                            │
   │  ③ 草稿标签？ 是的 → 跳过（并清除旧的本地记录）                                             │
   │  ④ rules.evaluate：归一化日期/时间/校区/人数 → 推算节次 → 48h/时段/午饭校验 → 四档判定        │
   └───────────────┬─────────────────────────────┬───────────────────────────┬────────────────┘
                   ▼                             ▼                           ▼
     applications/<id>.json           notify/pending/*.json             state.json
     （给「发起借用」的同学）           （给 qqbot → QQ 通知社员）         （agent 自己的记忆）
                   │                             │
        谷和平：读 JSON 提交教室借用     王恩成：读事件发 QQ 消息
```

---

## 1. 职责边界（谁做什么）

| 环节 | 负责人 | 交付物 | 状态 |
|---|---|---|---|
| 确定要素（读语雀 → 判定 → 打包 JSON） | 李赫 | 本 agent + 申请 JSON 契约 | ✅ 已完成 |
| webhook / 轮询常驻 + 本地持久化 | 李赫 | `serve` / `run` 命令 + `state.json` | ✅ 已完成 |
| 通知事件产出（QQ 通知的内容与时机） | 李赫 | `notify/pending/*.json` 契约 | ✅ 已完成（**投递**待做） |
| QQ 消息投递（语雀身份 → QQ 号映射、发送、重试） | 王恩成 | 消费 outbox 的 qqbot | ⏳ 待做（接口已留好） |
| 发起借用（拿 JSON 去借教室） | 谷和平 | 读 `applications/*.json` 提交的插件 | ⏳ 待做（契约已冻结） |
| 空闲教室预检 / 借用结果回写 | 谷和平 + 李赫 | —— | ⏳ 见 §9 待办 |

**本 agent 明确不做**：提交借用、查空闲教室、撤回申请、判断「学校是否审批通过」、
给社员发消息（只产出事件，不投递）。

---

## 2. 代码地图

```
src/nju_yuque/classroom/
├── rules.py     纯规则（无 IO）：草稿识别、字段解析、日期/时间/校区归一化、节次推算、四档判定
├── contract.py  对外契约：Application / Activity / SourceRef（pydantic）+ JSON Schema 导出
├── store.py     本地持久化 state.json：DocState / Meta / 原子写
├── notify.py    通知事件：Notice 模型、中文文案、文件 outbox、消费侧工具
├── pipeline.py  一轮处理的编排（唯一写文件的地方）+ Source 协议（便于测试）
├── server.py    webhook 报文解析 + HTTP 接收 + 轮询常驻 Runner
└── cli.py       yuque classroom once / run / serve / status / outbox / parse / schema / rules

docs/classroom-application.schema.json   申请 JSON 的机器可读契约（测试保证与代码同步）
examples/classroom_kb/{guide,template}.md 知识库里的《指导文档》与「文档模板」源文件
tests/test_classroom_*.py                142 个单测（全部离线、无网络，是主要的安全网）
```

设计上把 **规则 / IO / 持久化 / 通知** 四件事分开：`rules.py` 是纯函数（改动风险最高的业务规则
全在这一个文件里，改完跑 `pytest tests/test_classroom_rules.py` 就知道有没有踩到别人）。

---

## 3. 识别规则：「这份文档要不要处理？」

按顺序判断，**任何一条命中就跳过（skip）**，不产生申请、不发通知：

| 顺序 | 条件 | 说明 |
|---|---|---|
| 1 | 不是普通文档（`type != Doc`，如表格/数据表） | 数据集类文档不参与 |
| 2 | 是**指导文档** | 按 `doc_id` 锁定：首次自动识别（根目录 + 标题含「指导文档/填表说明」），之后写进 `state.json` 的 `meta.guide_doc_id`，**标题被改、被冒充都不认** |
| 3 | 在 `归档区` 子树内 | 归档 = 终点站，永不处理 |
| 4 | 标题恰好是 `审批日志` 且父节点是一篇文档 | 上一版设计遗留的子文档 |
| 5 | 不在 `--scope` 指定的目录子树内（可选） | 一个知识库混着别的文档时用 |
| 6 | **开头（前 3 个非空行）出现「草稿」二字** | 唯一的输入信号，见下 |

### 草稿标签（本设计的核心）

- 认「开头」= **前 3 个非空行**。写成 `【草稿】…`、`[草稿]`、`# 草稿`、`状态：草稿` 都认。
- 为什么只看开头：正文里偶然写一句「这是草稿阶段的策划」不至于把正式申请永久跳过。
- **草稿文档不入库**：`state.json` 里不会留下记录；社员先把标记删掉又加回来，旧的本地状态会被清掉。
- **已受理的文档加回草稿标记不算撤回**（终态不可逆），只会收到一条「改动无效」提醒。

---

## 4. 「确定要素」规则详解

### 4.1 字段

| 字段 | 必填 | 归一化能力 | 缺失时 |
|---|---|---|---|
| 申请人 | ✅ | —— | 用文档创建者姓名推断（`creator.name`） |
| 活动日期 | ✅ | `2026-09-16` / `9月16日` / `2026/9/16` / `09-16` | 退回 |
| 活动时间 | ✅ | `16:10-18:00` / `下午4点-6点` / `八点到九点` / `8:30~9:30` / `16时20-18时` | 退回 |
| 校区 | ✅ | 别名容错（`仙林校区`、`南京大学苏州校区`） | 退回 |
| 教学楼 | ➖ | 原样透传（见 §10 待办：映射成学校代码） | 空 = 随机 |
| 教室 | ➖ | 原样透传（学生端本来就指定不了具体教室） | 空 = 随机 |
| 人数 | ➖ | `30人` → `30` | 空 = 默认 30 |

还认两种「没用模板」的文档：正文里能提取到**日期 + 时间 + 校区**（如
`下周三 9月16日 下午 16:00-18:00 在仙林借个教室`）就照样受理，日志里注明「未使用模板」；
什么都提取不到 → 「无法识别」。

### 4.2 节次推算

- 真实课表（与学校一致）：第 1 节 `08:00-08:50` …… 第 7 节 `16:10-17:00` …… 第 12 节 `21:30-22:20`。
- 匹配时用**「一小时一档」**：档位起点 = 真实起点向下取到 30 分钟，档长 60 分钟。
  于是 `16:10-18:00` → 第 7-8 节，写 `16:00-17:00` 也认（容忍 10 分钟零头，不让社员背课表）。
- 跨午饭取并集：`11:00-15:00` → 第 4-5 节（**一次申请**，不要求拆成两单）。
- 社员**不需要填节次**，`period` 是 agent 推出来的。

### 4.3 四档判定

| 档位 | 触发 | 动作 |
|---|---|---|
| `ok` | 字段齐全、写法规范 | 打包 JSON + 通知「已受理」 |
| `normalized` | 写法不标准但能看懂（自动规范化） | 同上，JSON 里记 `normalizations` |
| `rejected` | 日期/时间/校区解析不了、信息缺失、标题不合格、超期、超时段、对不上节次 | 通知「退回修改」 |
| `unrecognized` | 既没模板、也提取不到日期+时间+校区 | 通知「无法识别」 |

**硬规则（会退回）**

- 提前量 **≥ 48 小时**（按活动真实开始时间算）；已过去的日期 → 「已经过去，无法借用」。
- 时间必须在 **08:00 – 22:20**；开始 ≥ 结束 → 退回（`17:00-16:00` 这种填反的）。
- **12:00–14:00 是吃饭时间，这段不需要借教室**（不是「违规」）：
  完全落在里面 → 退回；只跨一边 → 只借可用那段；两边都跨 → 一次申请覆盖（见上）。
- 标题为空 / 标题与系统文档重名（`指导文档`、`审批日志`、`归档区`…）/ 标题 > 60 字 → 退回。

**软提醒（照样受理，只打 WARNING）**

- 活动时长 ≥ 3 小时（怕社员把结束时间写错）。
- 12 小时制没写上下午：`八点到九点` → 按早上算并提醒；`两点到三点` → 凌晨不可能，
  按下午算并提醒（**这就是「分析」里那两条边缘情况**，都放行 + 提醒）。
- 人数 > 500。
- 活动日期距今 > 14 天。

> 判定细节都在 `rules.py`，每条规则都有对应单测（`tests/test_classroom_rules.py`）。

---

## 5. 输出契约 A：申请 JSON（交给谷和平）

### 5.1 位置与索引

```
<outdir>/applications/<application_id>.json    一份受理的申请一个文件，写后不再修改
<outdir>/applications/index.json               全部申请的索引（每轮扫目录重建，便于工具扫描）
```

- `application_id` = `<日期>-<节次带下划线>-<doc_id>`，如 `2026-09-16-7_8-1234567`。
  **稳定、可排序、可读**；同一篇文档重复处理不会换 id（但已受理的不会再处理）。
- `<outdir>` 默认 `~/.yuque/classroom/<group>_<slug>/`，可用 `--outdir` 改。
- 一份申请**只写一次**：受理时落盘，之后无论文档怎么被改都不会重写（改了只发提醒）。
  所以消费方可以放心「读一个文件 = 处理一次」。
- `index.json` 是**扫 `applications/*.json` 重建**的：源文档后来被删掉、本地记录被清掉，
  申请文件与索引里的一行都还在（消费方不会漏单）。

### 5.2 字段（完整定义见 `docs/classroom-application.schema.json`）

```json
{
  "schema_version": "1.0",
  "application_type": "classroom_borrow",
  "application_id": "2026-09-16-7_8-1234567",
  "packaged_at": "2026-09-12T09:00:00+08:00",
  "source": {
    "repo": "lqogh0/jsjysq",
    "doc_id": 1234567,
    "doc_slug": "bbf1n62",
    "doc_url": "https://nova.yuque.com/lqogh0/jsjysq/bbf1n62",
    "title": "新生见面会",
    "applicant": "张三",
    "applicant_raw": "张三",
    "creator_id": 77,
    "creator_login": "zhangsan",
    "creator_name": "张三",
    "content_hash": "sha256:1f3a…",
    "doc_updated_at": "2026-09-12T08:00:00+08:00"
  },
  "activity": {
    "name": "新生见面会",
    "date": "2026-09-16",
    "start": "16:10",
    "end": "18:00",
    "period": "7-8",
    "period_start": 7,
    "period_end": 8,
    "campus": "仙林",
    "campus_code": "3",
    "building": "",
    "room": "",
    "people": 30,
    "people_source": "default",
    "borrow_type": "团学活动"
  },
  "raw_fields": { "申请人": "张三", "活动日期": "9月16日", "活动时间": "下午4点-6点", "校区": "仙林校区", "教学楼": "", "教室": "", "人数": "" },
  "normalizations": ["日期「9月16日」→ 2026-09-16", "时间「下午4点-6点」→ 16:00-18:00"],
  "warnings": []
}
```

要点：

- **所有键名是英文**，值里的业务名词保留中文（`campus`、`borrow_type`）。
- `campus_code`：`1` 鼓楼 / `2` 浦口 / `3` 仙林 / `4` 苏州。
- `building` / `room` 是**社员原样填的自由文本**，空串表示「不限/随机」。
  ⚠️ 它是给人和给「意向教室」用的，**不是**学校教学楼的代码（待办见 §10）。
- `people_source` 说明人数是文档填的还是默认的 30。
- `normalizations` / `warnings` 是可读文案，**只用于展示和人工复核**，不要拿来做机器判断。
- 契约**只增不改**：新增字段必须给默认值并升 `schema_version`。

### 5.3 消费方（谷和平）怎么用

```python
import json, pathlib

for path in sorted(pathlib.Path("~/.yuque/classroom/lqogh0_jsjysq/applications").glob("*.json")):
    if path.name == "index.json":
        continue
    app = json.loads(path.read_text(encoding="utf-8"))
    a = app["activity"]
    # 提交（示例格式，具体看你插件的接口）：
    submit(
        {
            "活动名称": a["name"],
            "日期": a["date"],
            "节次": a["period"],
            "校区代码": a["campus_code"],
            "人数": a["people"],
            "意向教室": (a["building"] + a["room"]) or None,
        }
    )
    path.rename(processed_dir / path.name)  # 处理过就挪走，别重复提交
```

> 强烈建议：**处理一个挪一个**（或者用 `id` 记已处理集合），这样重复运行不会重复提交。

---

## 6. 输出契约 B：通知事件（交给王恩成 / qqbot）

### 6.1 目录协议

按周分组（详见 §8.7）：

```
<outdir>/notify/0914-0920/pending/000012-rejected-1234567-9f2c1a0b7e34.json   待投递
<outdir>/notify/0914-0920/done/000012-rejected-1234567-9f2c1a0b7e34.json      已投递（由 qqbot 挪进来）
<outdir>/notify/0914-0920/outbox.jsonl                                        本周只追加的审计流水
```

**消费约定**（给 qqbot 的实现者）：

1. 扫描 `pending/`，按文件名前缀 `seq` 从小到大处理（**只按 seq 排序，别解析其余部分**：
   末尾那串 `notice_id` 只是为了「state 重置后 seq 重来」时不互相覆盖）；
2. 一条消息发出去之后，把文件**移动**到 `done/`——移动成功即视为已投递；
3. 崩了重启就从 `pending/` 重新扫，天然 **至少一次**（宁可重复也别漏）；
4. 目录里是**同机共享**的，qqbot 与 agent 部署在同一台机器上最省事；
   若必须跨机，请用 `yuque classroom outbox list --json` 拉取、`outbox ack --seq N` 确认。
5. `outbox.jsonl` 用来审计（谁在什么时候收到过什么），**不要**既读它又挪文件，否则会重复投递。

### 6.2 事件字段

```json
{
  "schema_version": "1.0",
  "seq": 12,
  "notice_id": "9f2c1a0b7e34",
  "created_at": "2026-09-12T09:00:00+08:00",
  "kind": "rejected",
  "repo": "lqogh0/jsjysq",
  "doc": { "repo": "lqogh0/jsjysq", "doc_id": 1234567, "slug": "bbf1n62",
           "title": "新生见面会", "url": "https://nova.yuque.com/lqogh0/jsjysq/bbf1n62" },
  "member": { "yuque_id": 77, "yuque_login": "zhangsan", "name": "张三", "applicant_raw": "张三" },
  "summary": "「新生见面会」未通过校验，请修改后我会自动重审",
  "message": "❌ 「新生见面会」这份申请我没法处理，原因：\n1. ...\n\n改完把文档保存一下就行…",
  "reasons": ["活动开始时间 2026-09-14 08:00 距现在仅 23.0 小时，不足 48 小时"],
  "warnings": [],
  "application_id": "",
  "application_file": "",
  "extra": { "first_time": true, "fixes": [] }
}
```

- `summary` 一句话（适合当消息标题）；`message` 已渲染好的**中文正文，可直接发**。
- `reasons` / `warnings` 是结构化版本（想自己排版就用它）。
- **身份映射是 qqbot 的责任**：`member` 里给的是语雀侧身份（`yuque_id` / `yuque_login` / `name`）
  与文档里手填的 `applicant_raw`，三选一或组合着映射到 QQ 号，agent 不做这个映射。

### 6.3 六种事件与触发时机

| `kind` | 触发 | 建议的 QQ 文案要点 |
|---|---|---|
| `accepted` | 要素齐备（受理） | 已排队提交；附时间/节次/校区；提示「文档已锁定，改也无效」 |
| `rejected` | 判定不通过 | 逐条列原因；强调「改完保存就行，不用做别的动作」 |
| `unrecognized` | 看不出是申请 | 提示按模板新建文档 |
| `tampered` | **已受理**的文档又被改 | 强调「修改无效」；列改动字段（`标题：A → B`、`活动时间：… → …`） |
| `deleted_submitted` | 已受理的文档被删除 | 强调「删除 ≠ 撤回」；要改就新建一份并联系负责人 |
| `deleted_rejected` | 被退回/未识别的文档被删除 | 温和提示；如果你本就要重写，可忽略（见 §7 的开关） |

**去重策略**（避免刷屏，很重要）：

- `rejected` / `unrecognized`：只在**问题集合发生变化**时才重新通知。指纹只由
  **稳定问题码**（`too_soon` / `bad_campus` / `missing_field:校区` …）算出来，
  **不带会随时间变化的文案**——否则「距现在仅 47.0 小时」下一轮变成 46.5 小时就会
  每轮重新打扰社员。只改错别字、问题没变 → 不打扰。
- `tampered`：每次**内容变化**提醒一次（按正文哈希去重），改回原样也算一次变化。
- `deleted_*`：每篇文档只提醒一次，之后清掉本地记录（或留墓碑，见 §7）。
- `accepted`：一篇文档一次。

### 6.4 消费方（王恩成 / qqbot）怎么用

```python
import json, pathlib, shutil

pending = pathlib.Path(os.environ["CLASSROOM_OUTBOX"]) / "pending"
done = pending.parent / "done"

for path in sorted(pending.glob("*.json")):  # 文件名前缀是 seq，天然有序
    notice = json.loads(path.read_text(encoding="utf-8"))
    qq = lookup_qq(notice["member"])  # 你的身份映射
    if qq:
        send_qq(qq, notice["message"])  # message 可直接发
    else:
        alert_admin(notice)  # 映射不到就交给管理员
    done.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), done / path.name)  # 挪走 = 已投递
```

---

## 7. 本地持久化：`state.json`

```json
{
  "version": 1,
  "repo": "lqogh0/jsjysq",
  "meta": { "guide_doc_id": 123, "seq": 12, "rounds": 41, "last_round_at": "…" },
  "docs": {
    "1234567": {
      "doc_id": 1234567, "slug": "bbf1n62", "title": "新生见面会",
      "author_id": 77, "author_login": "zhangsan", "author_name": "张三",
      "status": "submitted",
      "content_hash": "sha256:1f3a…", "doc_updated_at": "2026-09-12T08:00:00+08:00",
      "first_seen_at": "…", "last_processed_at": "…",
      "application_id": "2026-09-16-7_8-1234567",
      "application_file": "/…/applications/2026-09-16-7_8-1234567.json",
      "problems": [], "warnings": [],
      "notified": { "accepted": "sha256:1f3a…" },
      "missing_rounds": 0, "deleted_at": "",
      "raw_fields": { "活动时间": "16:10-18:00" },
      "activity": { "date": "2026-09-16", "period": "7-8", "campus": "仙林", "...": "…" }
    }
  }
}
```

状态机（**agent 视角，语雀里看不到任何状态**）：

```
（无记录）──首次处理─┬─> submitted     要素齐备，已产出申请 JSON；终态，不再接受修改
                     ├─> rejected      要素不对，已通知社员；文档改了会重新审查
                     └─> unrecognized  看不出是申请，已通知社员；改了会重新审查

rejected / unrecognized ──社员加回草稿标记──> 删除本地记录（回到「无记录」）
submitted               ──社员加回草稿标记──> 仍是 submitted（草稿标记不能当撤回用）
任意状态                ──文档消失（webhook 事件 / 连续 N 轮看不到）──> 通知一次 → 删除记录
```

配套规则：

- **草稿文档不入库**（原文要求）。
- **改动检测**：先比 `doc.updated_at`（不变就不读正文，省 API 调用），变了再比
  `content_hash`（`sha256(标题 + 正文)`，标题变了也算改动）。
- **删除判定要三重确认**（怕误报给真人发错消息）：
  ① 快照必须可信（读取抛错、或「目录非空而文档列表为空」都当作快照失败，本轮不判删除）；
  ② `--delete-grace N`（默认 **2**）＝连续 N 轮看不到才进入下一步；
  ③ 再**单独读一次**那篇文档，只有 404 才算真被删（列表分页抖动、权限抖动都不算）。
  webhook 的删除事件**不必等防抖**，但**仍要单独读一次**：读得到就忽略，确实 404 才按删除处理
（真机压测抓到过：误信删除事件 → 丢记录 → 下一轮把同一篇又重新受理、社员收到两条「已受理」）。
动作映射：`publish/update/create` → 立即重读该文档；`delete/destroy/trash` → 上面的删除确认；
`comment/reply/unpublish` → 忽略；认不出来的 → 交给轮询兜底。
- **墓碑（默认开启）**：文档被判定删除后**保留**一条记录（写 `deleted_at`），不再重复通知。
  为什么默认保留：语雀回收站可以恢复文档（同一个 `doc_id` 回来），webhook 也可能把
  「移出目录 / 取消发布」误报成删除；留墓碑能让 agent 认出「这篇处理过」，
  **不会重复受理、重复通知、更不会覆盖已经交给下游的申请文件**。
  想彻底清干净用 `--purge-deleted`（删记录不影响 `applications/` 与 `notify/`，也不影响索引）。
- **`--notify-kinds`**：控制本进程只发哪些事件。例如
  `--notify-kinds accepted,rejected,unrecognized,tampered,deleted_submitted`
  ＝不提醒「被退回的文档被删除」（原文里留的那个「选项」）；`--notify-kinds none` ＝只落盘不通知。
- **重置**：想从头再来（比如换了一套规则）→ 停掉进程，删掉 `state.json`
  （**保留** `applications/` 与 `notify/`，否则会丢已产出的申请；seq 会从 1 重来，
  但通知文件名带 `notice_id`，不会覆盖还没投递的事件）。
  注意：删了 state 之后所有非草稿文档会被当新文档重新判定一次，已受理的会重新生成
  application（id 相同，文件覆盖，不影响下游幂等）。

---

## 8. 运行与部署

### 8.1 前置

```bash
uv tool install "nju-yuque"      # 或 uv run yuque ...
uv run yuque login --token <语雀令牌>   # 只读令牌就够（agent 不写语雀）
uv run yuque doctor --json              # 确认 mode=token
```

### 8.2 四条命令

```bash
# ① 跑一轮（cron / 手动）：默认真的执行，加 --dry-run 只看计划
uv run yuque classroom once --repo lqogh0/jsjysq
uv run yuque classroom once --repo lqogh0/jsjysq --dry-run
uv run yuque classroom once --repo lqogh0/jsjysq --json          # 机器可读报告
uv run yuque classroom once --repo lqogh0/jsjysq --force         # 无视 updated_at 全量重读

# ② 轮询常驻（没有公网入口时用）
uv run yuque classroom run --repo lqogh0/jsjysq --interval 60

# ③ webhook + 轮询兜底（推荐）
uv run yuque classroom serve --repo lqogh0/jsjysq \
    --listen 0.0.0.0 --port 8765 --path /yuque/webhook --secret <随机串> \
    --interval 60 --dump-webhook ./webhook-raw

# ④ 目录整理（唯一会写语雀的命令；默认 dry-run）
uv run yuque classroom tidy --repo lqogh0/jsjysq            # 只看计划
uv run yuque classroom tidy --repo lqogh0/jsjysq --apply    # 真整理
```

常用选项：

| 选项 | 作用 |
|---|---|
| `--outdir` | 输出目录（默认 `~/.yuque/classroom/<group>_<slug>/`） |
| `--notify` | 通知出口：`outbox`（默认）/ `console`（本地调试用）/ `none`，逗号可叠加 |
| `--notify-kinds` | 只发这些事件类型（见 §7） |
| `--scope <目录标题>` | 只处理某个目录子树内的文档（知识库混着别的文档时用） |
| `--guide <doc_id\|slug>` | 手动指定指导文档（首次可自动识别，之后按 id 锁定） |
| `--default-people` / `--borrow-type` | 人数缺省值 / 借用类型（默认 30 / 团学活动） |
| `--delete-grace` / `--purge-deleted` | 删除防抖轮数 / 判定删除后是否删掉本地记录（默认留墓碑） |
| `--tidy-toc` | 每轮顺手整理目录（见 §8.6）；默认**关**，只有它会让 agent 写语雀（仅移动目录节点） |

### 8.3 配置语雀 webhook（一次性）

1. 知识库 → **设置 → 开发者 / 消息推送** → 新建推送；
2. 地址填 `http(s)://<公网可达地址>/yuque/webhook`，把 `?token=<secret>` 拼在后面
   （或让语雀带 `X-Yuque-Token` 头），与 `--secret` 一致；
3. 勾选**文档发布 / 更新 / 删除**（评论类事件 agent 会忽略，勾了也无所谓）；
4. 先别急着上线：本地起一个 `serve`，用 `--dump-webhook ./raw` 抓一次真实报文，
   跑 `uv run yuque classroom parse ./raw/webhook-*.json` 确认解析结果符合预期；
5. 语雀要求**公网可达**（社区案例约 3s 超时）——所以接收端必须秒回：
   本实现收到报文后只往队列里塞，立刻返回 `{"ok":true}`，真正的处理在后台线程里做。

⚠️ **务必设置 `--secret`**：不带密钥时任何能访问该端口的人都能伪造成语雀事件
（伪造删除事件会让 agent 给社员发错消息）。agent 启动时会打印警告；
另外报文里如果带了知识库信息、且跟 `--repo` 不一致，事件会被直接忽略。
请求体超过 1 MiB 会被 413 拒绝，单连接读写超时 10 秒。

> 没有公网入口也完全能用：webhook 只是「更快」，**轮询才是可靠性来源**；
> 而且「文档被删除」基本只能靠轮询发现（webhook 的删除事件偶尔会漏）。

### 8.6 目录整理与「周切换」时间节点（`tidy`）

**目标形态**（一个知识库永远保持这个样子）：

```
教室借用申请
├── 指导文档（必读）        ← 根目录第一篇，按 doc_id 锁定
├── 0914-0920              ← 【唯一的活跃申请区】= 「今天」所在的那一周
└── 归档区                  ← 永远在最下方
    ├── 0921-0927          ← 内部按时间「最新在上」
    └── 0907-0913          ← 越往下越早
```

**时间节点**：`tidy` 的判据是「**活跃目录 = 今天所在的那一周（周一~周日）**」。
所以周切换天然发生在**周一 00:00 之后的第一次整理**：

| 时刻 | 发生的事情 |
|---|---|
| 周一 00:00 之后第一次跑 tidy | 上周目录 → 移进归档区（排在最上面）；本周目录不存在就新建；若本周目录之前被归档过（上周提前建好的「下周目录」）→ 从归档区搬回根目录；归档区重新置底 |
| 周中（周二~周日） | 已经符合目标形态 → **零操作、零 API 写入**（幂等，每轮跑也不会瞎折腾） |
| 社员在活跃目录里写申请 | 与 tidy 无关，agent 照常处理（归档区里的文档不再被处理） |

推荐部署（二选一）：

```bash
# A) 常驻时每轮顺手整理（只在真需要时才发请求）
uv run yuque classroom serve --repo lqogh0/jsjysq --secret xxx --tidy-toc

# B) 只让 cron 在周一凌晨整理一次（agent 保持完全只读）
0 0 * * 1  cd /path/to/NJU_Yuque && uv run yuque classroom tidy --repo lqogh0/jsjysq --apply
*/2 * * * *  cd /path/to/NJU_Yuque && uv run yuque classroom once --repo lqogh0/jsjysq
```

**安全边界**：

- tidy 只移动**目录节点（TITLE 分组）**，**不改任何文档正文、不建文档、不删文档**；
- 若被归档的目录里还有「已受理但未结案」的申请，会先**告警**（`⚠️「0914-0920」还有 N 份…`），但不会因此不动手；
- 不是 `MMDD-MMDD` 的目录（比如 `会议记录`）一律不碰；
- 根目录出现**重名**分组时直接放弃（不猜哪个是归档区）；
- 所有动作可 `--dry-run` 预览；实测可靠的原语只有「appendNode 带/不带 target_uuid」
  （`editNode + prev_uuid` 在语雀上是**静默失败**，代码里禁用）。

> 归档后，agent 不再处理归档区里的文档（§3 第 3 条）。因为 `state.json` / `applications/`
> 是本地记录，归档不会丢任何已产出的申请；只是它们不再被重新校验。

### 8.7 本地产出也按周分组

```
<outdir>/
├── applications/
│   ├── index.json               全量索引（消费方契约不变）
│   └── index-0914-0920.json     本周索引（新周自动出现）
├── notify/
│   ├── 0914-0920/{pending,done,outbox.jsonl}   ← 本周的通知事件
│   └── 0907-0913/{pending,done,outbox.jsonl}   ← 上周的（剩下的历史）
└── state.json
```

事件落在**自己 `created_at` 所属的那一周**，跨周那一刻写入的事件不会串周；
qqbot 扫 `notify/*/pending/*.json`（先按周、再按 seq）就不会漏投上周没投完的事件；
升级到本版时旧的扁平 `notify/pending` 会在下一轮**自动迁进**周目录。

- **Windows**：任务计划程序 → 触发器「登录时 / 系统启动时」→ 程序
  `powershell -Command "uv run yuque classroom serve --repo … --port 8765"`；
  注意别勾「不管用户是否登录都运行」（会拿不到 `~/.yuque/auth.json`）。
- **Linux / 服务器**：写一个 systemd unit（`Restart=always`）跑 `serve`；
  或用 `once` + cron `*/2 * * * *`（最省资源，代价是最多 2 分钟的延迟）。
- **单进程约束**：同一个 `--outdir` **只跑一个进程**（`state.json` 不做跨进程加锁）。
  要并行就用不同的 `--outdir`。
- 进程内已经做了兜底：单轮异常不会打死常驻循环，会打日志后继续。

### 8.5 运维命令

```bash
uv run yuque classroom status --repo lqogh0/jsjysq        # 本地状态概览（不联网）
uv run yuque classroom status --repo … --json             # 机器可读
uv run yuque classroom outbox list --repo …               # 还没投递的通知
uv run yuque classroom outbox ack --seq 12 --repo …       # 确认某条（挪到 done/）
uv run yuque classroom outbox purge --repo …              # 清空 done/
uv run yuque classroom rules                              # 打印生效中的业务规则
uv run yuque classroom schema --out docs/classroom-application.schema.json
```

---

## 9. 交接清单（三个人的具体 TODO）

### 给王恩成（qqbot）

1. 定目录：与 agent 约定 `CLASSROOM_OUTBOX`（默认 `~/.yuque/classroom/<repo>/notify`）。
2. 实现轮询 `pending/` → 发 QQ → `shutil.move` 到 `done/`（伪代码见 §6.4）；
   文件名形如 `000012-rejected-1234567-9f2c1a0b7e34.json`，**只按前缀 seq 排序**。
3. 做**语雀身份 → QQ 号**的映射表（用 `member.yuque_login` / `name` / `applicant_raw` 匹配）。
   映射不到时不要静默丢掉，转给管理员一份清单。
4. `message` 可以直接发；如果 QQ 侧要卡片/按钮，用 `reasons` / `warnings` / `kind` 自己排版。
5. 目前**只在 agent 侧**做了去重（同一种问题不重复发）。若 qqbot 会重试，
   请按 `notice_id` 做幂等。
6. 微信接入的可行性结论如果与「文件 outbox」冲突，回头改 `notify.py` 里的
   `build_notifier` 加一个出口即可——**agent 侧只认 `Notifier` 协议**。

### 给谷和平（发起借用）

1. 消费 `<outdir>/applications/*.json`（契约见 §5 与 `docs/classroom-application.schema.json`）。
2. 建议「处理一个挪一个」，或按 `application_id` 记已处理集合，保证幂等。
3. `building` / `room` 是自由文本，要落到学校表单的**教学楼代码**需要一次映射
   （见 §10 待办 1）；不映射就当「随机教室」处理。
4. 「空闲教室预检」目前**不在 agent 里**：agent 不做实时余量校验（它只读知识库，
   不该为每份申请去查一次空闲教室）。需要的话按 §10 待办 2 的钩子接。
5. 审批结果回写（`SQBH` / `SHZT`）**这一版不做**（上一版的 `--sync-status` 已随旧实现删除）。
   要恢复请新开一个只读命令，仍然不要改语雀文档。

### 给 CAC / 运维

1. 建/改知识库时把 `examples/classroom_kb/template.md` 贴进「文档模板」，
   把 `guide.md` 更新到指导文档里（社员看到的规则以它为准）。
2. 决定部署位置（有公网就用 `serve`，没有就用 `run` / cron `once`）。
3. 语雀令牌只要**只读**权限；`~/.yuque/auth.json` 是敏感文件，别外传。
4. 上线前的标准动作：
   ```bash
   uv run yuque classroom once --repo <repo> --dry-run     # 看判定是否符合预期
   uv run yuque classroom once --repo <repo> --notify console
   # 确认无误后再放开 --notify outbox（默认）与 webhook
   ```

---

## 10. 已知限制与待办

| # | 事项 | 现状 | 建议 |
|---|---|---|---|
| 1 | **教学楼名 → 学校代码** | `building` 原样透传自由文本，不映射 | 加一个 `BuildingResolver` 钩子（可复用 `crb buildings --campus N --json` 的字典），映射成功才写代码，失败就当随机 |
| 2 | **空闲教室预检** | 没做（原文里「目标教学楼在目标时段无空闲教室 → 退回」这条未实现） | 在 `pipeline._accept` 前插一个可选 checker（只读查空闲教室），返回「无教室」则改判 `rejected` |
| 3 | **借用结果回写/撤回** | 没做 | 独立命令 + 独立状态文件，仍不改语雀文档；或由谷和平那一侧管理 |
| 4 | **QQ 身份映射** | 没做（王恩成） | 见 §9 |
| 5 | **节假日 / 学校封楼** | 没考虑 | `rules.py` 加一张日期黑名单即可 |
| 6 | **webhook 报文格式** | 语雀没有公开文档，解析器按经验宽容匹配 | 用 `--dump-webhook` 抓真实报文后收紧 `server.parse_webhook` |
| 7 | **目录整理的部署** | `tidy` 已实现（§8.6），但需要你决定谁来触发：常驻 `--tidy-toc` 还是 cron 周一跑一次 |
| 8 | **删除检测** | 连续 N 轮（默认 2）看不到才判定 | 若知识库文档量大、`yuque docs` 分页偶发失败，就把 `--delete-grace` 调大 |
| 9 | **多进程** | 不支持并发写同一 `state.json` | 一个 outdir 一个进程 |
| 10 | **时区** | 固定 UTC+8（`rules.CN`） | 部署在哪都一样，不用改 |

---

## 11. 从旧版迁移（重要）

上一轮的实现（`examples/classroom_application_agent.py`，已删除）与本版**设计冲突**，切换时注意：

| 旧版行为 | 新版 |
|---|---|
| 靠文档里 `状态：待提交/已提交/已退回` 驱动 | ❌ 不再写状态；社员只维护「开头有没有草稿标记」 |
| agent 建 `审批日志` 子文档写结论 | ❌ 删除；结论走 QQ 通知（`notify/pending/`） |
| agent 把文档移到 `0914-0920` 周目录 | ❌ 不动目录（周目录变成纯人工/另写脚本） |
| agent 删除「不像申请」的文档 | ❌ 不删任何文档；改判 `unrecognized` + 通知本人 |
| `--approve` / `--sync-status` 回写审核结果 | ❌ 删除（见 §10 待办 3） |
| 日期必须在「当前周或下一周」否则退回 | ⚠️ 改成 **> 14 天只提醒**（不再强绑周目录） |
| 标题必须像活动名、不能冒充系统文档 | ✅ 保留（会退回） |

**迁移步骤**：

1. 把 `guide.md` 更新到新流程（社员要知道「删掉草稿标记」这件事，且旧的状态字段不再需要）；
2. 老文档里残留的 `状态：xxx` 行**不影响**新版（新版只解析它认识的字段），
   但建议在群里说一声：以后填完删掉 `【草稿】` 那一行就行；
3. 旧的 `审批日志` 子文档不会被处理（§3 第 4 条跳过），可以留着当历史，也可以删掉；
4. 首次上线建议 `--dry-run` 跑一轮，人工核对判定表，再打开通知。

---

## 12. 排错 FAQ

| 现象 | 先看哪里 |
|---|---|
| 社员说「没收到通知」 | `yuque classroom outbox list` 有没有堆积 → 有堆积就是 qqbot 没消费；没有堆积说明文档被 skip 了 → `once --dry-run --json` 看 `skipped` 计数与原因 |
| 申请 JSON 没生成 | `status` 看该文档是 `rejected` 还是 `unrecognized`，`problems` 里写着原因 |
| 一直重复通知同一个人 | 检查 `notify-kinds` 是否被改过；`rejected` 的重发条件是「问题集合变了」 |
| 文档被误判为删除 | 现在有三重确认（快照可信 + 防抖 N 轮 + 单独读 404），正常不会；若仍发生请贴日志，并检查 `yuque docs --repo …` 能否完整拉到列表 |
| webhook 不生效 | `parse` 一下抓到的原始报文（`--dump-webhook`），看是 `action_type` 不认还是没带 doc_id；都没有就靠轮询 |
| 想重来一次 | 停进程 → 备份并删掉 `state.json` → 重跑（别删 `applications/`、`notify/`） |
| 判定规则不符合预期 | `rules.py` + `tests/test_classroom_rules.py`；改完跑 `uv run pytest -q` |
| 契约要加字段 | `contract.py` + 重新生成 schema（`yuque classroom schema --out …`）+ 升 `SCHEMA_VERSION` |

---

## 12.1 真机压测记录（2026-09-19，`lqogh0/jsjysq`）

在真实知识库上跑了一遍 `scripts/stress_classroom.py --allow-real-writes --burst 25`：

| 项目 | 结果 |
|---|---|
| 14 种申请样例判定 | 14/14 与预期一致（7 受理 / 5 退回 / 1 无法识别 / 1 草稿不处理） |
| 批量灌入 | 25 篇 → 12.1s 内全部受理 |
| webhook | 正常事件 200；错密钥 401；跨库事件忽略；「文档还在」的删除事件忽略；真删 → 立即通知 |
| 已受理后被改 | 收到「改动无效」告警，状态仍为终态 |
| 文档被删 | 轮询约 15s 判定（防抖 2 轮）→ 通知 + 立墓碑；申请文件与索引仍在 |
| 幂等 | 复跑通知数 41 → 41（不重复打扰） |
| **对语雀只读** | 抽查 25 篇文档 `updated_at` 全部未被改动 → agent 0 次写操作 |
| 收尾 | 39 篇测试文档全部删除，知识库恢复原样（保留新增的 `0921-0927` 目录） |

这一轮压测抓出并修掉 2 个真问题：
**① webhook 删除事件曾被直接采信**（文档其实还在 → 丢记录 → 下一轮重复受理，社员收到两条「已受理」）
→ 现在必须单独读 404 才认，且删除后默认留墓碑；
**② 压测脚本误以为 `PUT /toc` 返回新建节点**（真实 API 返回整棵目录）→ 测试文档一度被挂到指导文档
下面，现已改为回查确认（写后延迟要重试）。

> 还没验证的：**语雀真的往 agent 推 webhook**（需要公网可达 URL）。接收端本身已用本机 HTTP 事件
> 验证过；配好公网地址后按 §8.3 打开即可。

---

## 13. 开发

```bash
uv sync --dev
uv run ruff check . && uv run ruff format --check .
uv run pytest -v                       # 全量（含 140+ 个 classroom 用例，全部离线）
uv run pytest tests/test_classroom_pipeline.py -v
uv run python scripts/sync_skill.py    # 改完 SKILL.md 后同步仓库内副本
```

### 端到端压测（改完 pipeline / server 后建议跑一遍）

```bash
# ① 本地：起一个「仿真语雀」，真 CLI + 真 HTTP + 真 webhook 全链路跑 26 项检查
uv run python scripts/stress_classroom.py
#    ├─ scripts/mock_yuque.py —— 本地仿真语雀 API（读 + 建/改/删文档 + 服务端写操作计数）
#    └─ 会断言「agent 全程 0 次语雀写操作」（用服务端写计数反推）

# ② 真机（会创建测试文档并在结束时删除；加 --keep 可保留）
uv run python scripts/stress_classroom.py --repo lqogh0/jsjysq --host https://nova.yuque.com     --allow-real-writes --burst 25 --interval 5
#    ├─ 只读观察（不建不删）：去掉 --allow-real-writes
#    └─ 真机模式的「只读性」用「批量文档 updated_at 是否被改动」验证
```

覆盖：14 种脏数据判定、批量灌入、幂等复跑、webhook（正常/错密钥/跨库/假删除/真删除）、
已受理后被改（改动告警）、文档被删（三重确认 + 墓碑）、申请文件与索引不受删除影响。

改了 `contract.py` 的模型后**必须**重新生成 schema，否则 `test_json_schema_is_shipped_and_in_sync` 会红。
