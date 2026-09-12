# 语雀 API 逆向/实测笔记

> 面向维护者。记录本项目实际踩过、验证过的语雀接口行为，避免重复试错。
> 探测过程与结论汇总见 CRB 仓库的 `docs/reference/语雀-agent-可行性探测.md`。

---

## 1. 认证与 scope

| 模式 | 头 / Cookie | 稳定性 |
|---|---|---|
| 官方 OpenAPI | `X-Auth-Token: <令牌>`，全部 `/api/v2/*` | 官方接口，长期有效 |
| 网页内部接口 | Cookie（`_yuque_session` + `yuque_ctoken`）+ `X-CSRF-Token` | 非公开，约 2 周过期 |

**scope 命名规则（关键）**：

- 只读：`doc:read`、`repo:read`、`group:read`、`statistic:read`
- 读写：`doc`、`repo`、`group`（**没有 `:read` 后缀**）
- 部分令牌会写成 `doc:write`

`YuqueApi.can_write(kind)` / `session.scope_allows_write()` 就是按「无 `:read` 即写」判断的。

**团队令牌会自动识别团队**：`GET /api/v2/user` 对团队令牌返回的是 `type=Group` 的对象，
其 `login` 才是真正的团队 login。如果本地默认 group 与令牌不符（例如默认 `ghxd00`
但令牌属于 `lqogh0`），所有接口都会 404。`login_token()` 以服务端返回为准覆盖 group。

**令牌不能跨团队**：`lqogh0` 的令牌拿不到 `ghxd00` 的数据，反之亦然。

---

## 2. 官方 OpenAPI 实测行为

### 2.1 列表分页

| 接口 | 分页参数 | 上限 |
|---|---|---|
| `GET /api/v2/repos/{id}/docs` | `limit` + `offset` | **limit 最大 100**，`limit=500` 报 422 |
| `GET /api/v2/groups/{login}/repos` | `limit` | 100 |
| `GET /api/v2/groups/{login}/users` | `offset` | PageSize 固定 100 |

> `GET .../groups/{login}/users` 返回的成员数可能少于 `statistics.member_count`
> （实测 89 vs 290），不要用前者当「团队总人数」。

### 2.2 文档详情

`GET /api/v2/repos/{book_id|group/slug}/docs/{id|slug}?raw=1`

- 路径参数同时接受**数字 id** 和 **slug**。
- 返回字段：`format`、`body`（**正文 Markdown**）、`body_draft`、`body_html`、`body_lake`、
  `tags`、`creator`、`book`、`comments_count`、`word_count`。
- `format` 取值：`lake`（普通文档，但 `body` 仍是 Markdown）、`markdown`、`lakesheet`（表格）。

### 2.3 搜索

`GET /api/v2/search?q=&type=doc|repo&page=&scope=<group>/<book_slug>`

- 响应是 `{"meta": {...}, "data": [...]}`，**meta 不在 data 里**，客户端要单独保留。
- `scope` 必须是 `group/book_slug`（只写 `book_slug` 会 400）。
- 索引有延迟：新发布文档未必立刻可搜 → 增量检测请用「列文档比 `updated_at`」。

### 2.4 目录字段的空值

`GET /api/v2/repos/{id}/toc` 对 `TITLE` 节点返回 `doc_id: ""`、`level: ""`；
根节点的 `parent_uuid` 是 `null`。模型层必须容错（见 `models.TocItem` 的 validator）。

---

## 3. 表格（Sheet / lakesheet）

表格文档 `body` 形如：

```json
{"format": "lakesheet", "version": "3.5.5", "larkJson": true,
 "sheet": "<zlib 压缩流被按 latin-1 解码后的字符串>", "...": "..."}
```

解码链：

```python
sheets = json.loads(zlib.decompress(body_json["sheet"].encode("latin-1")).decode("utf-8"))
# sheets[i]["data"][行]["列"] = {"v": 值, "s": 样式, "t": 类型}
```

- `sheet` 字段前 4 字节是 `78 9c`（zlib 魔数），可用它做格式探测。
- 已实测可解出表头、长文本、换行；整行空白需跳过。
- 实现在 `lakesheet.py`，有离线单测（`tests/test_lakesheet.py`）。

---

## 4. 写接口（官方）

### 4.1 文档

| 操作 | 请求 |
|---|---|
| 新建 | `POST /api/v2/repos/{repo}/docs`，体 `{title, slug?, body(必填), format="markdown", public?}` |
| 更新 | `PUT /api/v2/repos/{repo}/docs/{id}`，只传要改的字段 |
| 删除 | `DELETE /api/v2/repos/{repo}/docs/{id}` |

- `public`：`0` 私密 / `1` 公开 / `2` 企业内公开。
- 只读令牌会返回 `401 {"message":"请给此 Token 添加 doc 权限"}`。

### 4.2 目录（容易踩坑）

`PUT /api/v2/repos/{repo}/toc`，必填 `action` + `action_mode`。

| 场景 | 载荷 |
|---|---|
| 挂文档到根 | `{action:"appendNode", action_mode:"child", type:"DOC", doc_ids:[id]}` |
| 挂文档到父节点 | 同上 + `target_uuid:"<父节点 uuid>"` |
| 建分组 | `{action:"appendNode", action_mode:"child", type:"TITLE", title:"..."}` |
| 移除节点 | `{action:"removeNode", action_mode:"sibling|child", node_uuid:"..."}` |

实测结论：

1. **`action_mode` 一律用 `child`**。`sibling` + `target_uuid` 不生效（返回 200 但没挂上）；
   `TITLE` 节点用 `sibling` 也建不出来。
2. **目录读取有写后延迟**（秒级）：`PUT` 之后立刻 `GET /toc` 可能看不到新节点。
   不要据此判定失败并重试 —— 会重复挂载。
3. `removeNode` 只移除目录节点，**不删除关联文档**；`action_mode=child` 连子节点一起移除。
4. 新建文档**不会自动进目录**，需要额外调一次 TOC 接口。

### 4.3 知识库

| 操作 | 请求 |
|---|---|
| 新建 | `POST /api/v2/groups/{login}/repos`，体 `{name(必填), slug(必填), description?, public?, enhancedPrivacy?}` |
| 删除 | `DELETE /api/v2/repos/{id 或 group/slug}` |

⚠️ 删除知识库会连同内部所有文档一起消失。

---

## 5. 网页内部接口（Cookie 模式）

社区验证（`yuque-cli`、`yuque-mcp` 等）与本项目实现：

| 能力 | 端点 |
|---|---|
| 身份 / 知识库 | `GET /api/mine`、`GET /api/mine/books`、`GET /api/mine/group_quick_links` |
| 文档列表 | `GET /api/docs?book_id=&offset=` |
| 文档详情 | `GET /api/docs/{slug_or_id}?book_id=&mode=markdown`（正文在 `sourcecode`） |
| 渲染前 Markdown | `GET /{space}/{repo}/{slug}/markdown?plain=true` |
| 评论 | `GET/POST /api/comments`、`DELETE /api/comments/{id}`、`POST /api/comments/finish` |
| 搜人（@用） | `GET /api/users/complete?target_type=Doc&target_id=&group_id=&q=` |
| 附件 | `POST /api/upload/attach` |

写操作四要素：

1. Cookie 带 `_yuque_session` 与 `yuque_ctoken`；
2. 头 `X-CSRF-Token: <yuque_ctoken>`；
3. 头 `X-Requested-With: XMLHttpRequest`，body 是 **JSON**；
4. 带正确的 `Referer`。

**游客也会拿到 `_yuque_session`**，所以判断登录成功必须真的请求一次 `/api/mine`。

---

## 6. 事件订阅（Webhook）

`知识库 → 设置 → 开发者 / 消息推送` 可订阅：发布/更新/删除文档、评论增删改、回复增删改。

- 推送为 `POST` + JSON，`data` 内直接带 `body`（Markdown 源码）、`title`、`book`、
  `actor_id`、`action_type`、`path`。
- 需要**公网可达 URL**；接收端要快速响应（社区案例约 3s 超时）。
- 这是「新申请出现就触发 agent」的官方手段，不必轮询。

---

## 7. 语雀没有的能力（能力边界）

- **官方 OpenAPI 没有评论接口**，也没有站内私信/群通知 → 「通知某人」只能
  ①Cookie 模式评论 + @ ②写一份退还意见文档 + 人工转达 ③接入外部 IM。
- 没有「表单」能力；填表载体只能是文档或表格（Sheet）。
- 「数据表 / 多维表格」尚未验证（团队里没有样本，官方也没有专门端点）。

---

## 8. 限流与错误

- 观测到响应头 `x-ratelimit-limit: 0`，连续 30 次请求无 429；批量拉取建议仍自行限速。
- 错误体形如 `{"status": 4xx, "message": "..."}`；客户端按状态码映射异常：
  `401 → AuthExpiredError`、`403 → InsufficientScopeError`、`404 → NotFoundError`、
  `429 → RateLimitedError`。
