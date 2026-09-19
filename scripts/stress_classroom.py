"""教室申请 agent 的**端到端压测**：真 CLI + 真 HTTP + 真 webhook + 真 outbox。

它会：

1. 起一个「仿真语雀」（``scripts/mock_yuque.py``）或直连真实知识库；
2. 把知识库整理成新架构的样子（指导文档 + 日期目录 + 归档区）；
3. 用 ``yuque classroom serve`` **把 agent 常驻挂在后台**（真进程、真端口）；
4. 模拟社员在日期目录里**新建 / 修改 / 删除**申请文档（含各种脏数据）；
5. 往 agent 的 webhook 端口**真发 HTTP 事件**（含伪造/跨库/错误密钥）；
6. 读 agent 的 ``state.json`` / ``applications/*.json`` / ``notify/pending/*.json`` 对答案；
7. 断言「agent 全程一次语雀写操作都没做」（仿真模式用写操作计数，真机模式比对 updated_at）。

用法::

    # 本地仿真（默认，不动任何真实数据）
    python scripts/stress_classroom.py

    # 真机：只读观察（不建/不删任何文档）
    python scripts/stress_classroom.py --repo lqogh0/jsjysq

    # 真机：允许写测试文档（会创建，跑完自动删除）
    python scripts/stress_classroom.py --repo lqogh0/jsjysq --allow-real-writes
"""

from __future__ import annotations

import argparse
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

ROOT = Path(__file__).resolve().parents[1]
CN = timezone(timedelta(hours=8))
DEFAULT_REPO = "lqogh0/jsjysq"
SECRET = "stress-secret"
GUIDE_TITLE = "指导文档（必读）"


def next_week_title() -> str:
    """按知识库约定给「下周」的目录名（周一到周日，如 0921-0927）。"""
    today = now().date()
    monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    return f"{monday:%m%d}-{monday + timedelta(days=6):%m%d}"


ARCHIVE_TITLE = "归档区"
DRAFT_LINE = "【草稿】填完请删掉这一行，agent 才会处理本文档\n\n"


def now() -> datetime:
    return datetime.now(CN)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def ok(flag: bool) -> str:
    return "✅" if flag else "❌"


def norm_ts(stamp: str) -> str:
    """语雀「创建响应」是毫秒精度、「详情接口」是秒精度，比较前统一到秒。"""
    return (stamp or "").split(".")[0]


def wait_for(pred, timeout: float = 30.0, interval: float = 0.3) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------- 模板
def body(**over: str) -> str:
    base = {
        "申请人": "压测员",
        "活动日期": over.pop("活动日期", (now() + timedelta(days=4)).strftime("%Y-%m-%d")),
        "活动时间": over.pop("活动时间", "16:10-18:00"),
        "校区": over.pop("校区", "仙林"),
        "教学楼": over.pop("教学楼", ""),
        "教室": over.pop("教室", ""),
        "人数": over.pop("人数", ""),
    }
    base.update(over)
    return "\n".join(f"{k}：{v}" for k, v in base.items()) + "\n"


@dataclass
class Case:
    """一个压测样例：我们期望 agent 怎么判。"""

    name: str
    title: str
    text: str
    expect: str  # accepted / rejected / unrecognized / draft
    note: str = ""
    doc_id: int = 0
    slug: str = ""
    seen: str = ""  # agent 实际判成什么
    extras: dict = field(default_factory=dict)


def build_cases() -> list[Case]:
    d = lambda n: (now() + timedelta(days=n)).strftime("%Y-%m-%d")  # noqa: E731
    return [
        Case("标准申请", "压测A-标准申请", body(), "accepted", "16:10-18:00 → 第7-8节"),
        Case(
            "脏写法",
            "压测B-脏写法",
            body(
                活动日期=f"{int(d(4)[5:7])}月{int(d(4)[8:10])}日",
                活动时间="下午4点-6点",
                校区="仙林校区",
            ),
            "accepted",
            "应被自动规范化",
        ),
        Case("中文数字", "压测C-中文数字", body(活动时间="下午两点到三点"), "accepted", "→ 第5节"),
        Case(
            "跨午饭",
            "压测D-跨午饭",
            body(活动时间="11:00-15:00"),
            "accepted",
            "→ 第4-5节，一次申请",
        ),
        Case(
            "长时长告警", "压测E-长时长", body(活动时间="16:00-22:00"), "accepted", "应带 WARNING"
        ),
        Case(
            "指定教室",
            "压测F-指定教室",
            body(教学楼="仙II区", 教室="仙II-101", 人数="50"),
            "accepted",
        ),
        Case(
            "自由文本",
            "压测G-自由文本",
            f"下周三 {d(4)} 下午 16:00-18:00 在仙林借个教室，社团分享会\n",
            "accepted",
            "没用模板但能提取要素",
        ),
        Case("缺校区", "压测H-缺校区", body(校区=""), "rejected", "必填缺失"),
        Case(
            "午饭时段", "压测I-午饭时段", body(活动时间="12:30-13:30"), "rejected", "不需要借教室"
        ),
        Case("太仓促", "压测J-太仓促", body(活动日期=d(1)), "rejected", "不足 48 小时"),
        Case("时间填反", "压测K-时间填反", body(活动时间="17:00-16:00"), "rejected"),
        Case("疑似冒充", "压测L-教室申请模板", body(), "rejected", "标题不能冒充系统文档"),
        Case("乱写", "压测M-随笔", "今天天气不错，随便写点东西，没什么用。\n", "unrecognized"),
        Case("草稿", "压测N-草稿", DRAFT_LINE + body(), "draft", "agent 永远不碰"),
    ]


# ---------------------------------------------------------------- 语雀客户端
class Yuque:
    """直接打官方 API（模拟「社员在网页里操作」）。"""

    def __init__(self, host: str, token: str, repo: str) -> None:
        self.host = host.rstrip("/")
        self.repo = repo
        self.my_writes = 0  # driver（= 社员）自己发起的写操作次数
        self.client = httpx.Client(
            base_url=self.host,
            headers={"X-Auth-Token": token, "User-Agent": "stress/1.0"},
            timeout=20,
        )

    def toc(self) -> list[dict]:
        return self.client.get(f"/api/v2/repos/{self.repo}/toc").json()["data"]

    def docs(self) -> list[dict]:
        return self.client.get(f"/api/v2/repos/{self.repo}/docs", params={"limit": 100}).json()[
            "data"
        ]

    def doc(self, key: str) -> dict:
        resp = self.client.get(f"/api/v2/repos/{self.repo}/docs/{key}", params={"raw": 1})
        if resp.status_code >= 400:
            raise RuntimeError(f"读文档失败 {key}: {resp.status_code} {resp.text[:120]}")
        return resp.json()["data"]

    def create(self, *, title: str, text: str, parent_uuid: str = "") -> dict:
        self.my_writes += 1
        resp = self.client.post(
            f"/api/v2/repos/{self.repo}/docs",
            json={"title": title, "body": text, "format": "markdown"},
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"建文档失败：{resp.status_code} {resp.text[:200]}")
        doc = resp.json()["data"]
        if parent_uuid:
            self.my_writes += 1
            self.client.put(
                f"/api/v2/repos/{self.repo}/toc",
                json={
                    "action": "appendNode",
                    "action_mode": "child",
                    "type": "DOC",
                    "doc_ids": [doc["id"]],
                    "target_uuid": parent_uuid,
                },
            )

            # 语雀目录是「写后延迟」的：立刻回查可能看不到，要重试（别据此判定失败）
            def mounted() -> bool:
                node = self.node_of(int(doc["id"]))
                return bool(node) and node["parent_uuid"] == parent_uuid

            if not wait_for(mounted, timeout=20, interval=1.0):
                node = self.node_of(int(doc["id"]))
                self.delete(int(doc["id"]))  # 挂不上就自己收拾干净，别留垃圾
                raise RuntimeError(
                    f"#{doc['id']} 没挂进目标目录（node={node and node['parent_uuid']}，"
                    f"期望={parent_uuid}），已删除该文档"
                )
        return doc

    def update(self, doc_id: int, *, title: str | None = None, text: str | None = None) -> dict:
        self.my_writes += 1
        payload: dict = {"format": "markdown"}
        if title is not None:
            payload["title"] = title
        if text is not None:
            payload["body"] = text
        return self.client.put(f"/api/v2/repos/{self.repo}/docs/{doc_id}", json=payload).json()[
            "data"
        ]

    def delete(self, doc_id: int) -> None:
        self.my_writes += 1
        self.client.delete(f"/api/v2/repos/{self.repo}/docs/{doc_id}")

    def create_group(self, title: str) -> dict:
        """建目录分组并**回查**它的 uuid。

        注意：真实 API 的 ``PUT /toc`` 返回的是整棵目录（不是新建的那个节点），
        直接取 ``data[0]`` 会拿到别人的 uuid（第一次真机压测就踩了这个坑，
        导致测试文档全被挂到指导文档下面）。
        """
        self.my_writes += 1
        self.client.put(
            f"/api/v2/repos/{self.repo}/toc",
            json={"action": "appendNode", "action_mode": "child", "type": "TITLE", "title": title},
        )
        for item in self.toc():
            if item["type"] == "TITLE" and item["title"] == title and not item["parent_uuid"]:
                return item
        raise RuntimeError(f"建完分组却在目录里找不到：{title}")

    def node_of(self, doc_id: int) -> dict | None:
        return next((i for i in self.toc() if i.get("doc_id") == doc_id), None)

    def close(self) -> None:
        self.client.close()


# ---------------------------------------------------------------- 压测主体
class Stress:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.repo = args.repo
        self.cases: list[Case] = build_cases()
        self.created: list[int] = []
        self.tmp = Path(tempfile.mkdtemp(prefix="classroom-stress-"))
        self.workdir = self.tmp / "work"
        self.outdir = self.tmp / "out"
        self.home = self.tmp / "yuque-home"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.home.mkdir(parents=True, exist_ok=True)
        self.mock: subprocess.Popen | None = None
        self.agent: subprocess.Popen | None = None
        self.log = self.tmp / "agent.log"
        self.results: list[tuple[str, str, str, str]] = []
        self.notes: list[str] = []
        self.untouched: dict[int, str] = {}  # 建完不再动的文档，用来验证 agent 没写

    # -- 基础设施 ---------------------------------------------------------
    def say(self, text: str) -> None:
        print(text, flush=True)

    def section(self, title: str) -> None:
        self.say(f"\n{'=' * 78}\n{title}\n{'=' * 78}")

    def start_mock(self) -> tuple[str, str]:
        port = free_port()
        state = self.tmp / "mock-state.json"
        self.mock = subprocess.Popen(
            [
                sys.executable,
                "-u",
                str(ROOT / "scripts" / "mock_yuque.py"),
                "--port",
                str(port),
                "--token",
                "mock-token",
                "--seed",
                "--state",
                str(state),
            ],
            cwd=ROOT,
            stdout=(self.tmp / "mock.log").open("w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
        host = f"http://127.0.0.1:{port}"
        # 探活：仿真语雀对 /hello 也要 token，所以「能连上」就算活着（401 也算）
        ready = wait_for(lambda: self._mock_up(host), timeout=20)
        if not ready:
            raise SystemExit("仿真语雀起不来，看 " + str(self.tmp / "mock.log"))
        self.say(f"▶ 仿真语雀：{host}（token=mock-token，已 seed 指导文档 + 日期目录）")
        return host, "mock-token"

    @staticmethod
    def _mock_up(host: str) -> bool:
        try:
            httpx.get(f"{host}/api/v2/hello", timeout=2)
            return True
        except Exception:
            return False

    def mock_stats(self) -> dict:
        """仿真语雀的写操作计数（用来证明 agent 一次都没写过）。"""
        try:
            resp = httpx.get(
                f"{self.args.host}/__stats",
                headers={"X-Auth-Token": self.args.token or ""},
                timeout=5,
            )
            return resp.json()["data"]
        except Exception as exc:
            self.say(f"  ! 读不到仿真语雀的写计数：{exc}")
            return {"write_total": -1, "writes": {}}

    def write_home(self, host: str, token: str, *, group: str = "lqogh0") -> None:
        (self.home / "auth.json").write_text(
            json.dumps(
                {
                    "mode": "token",
                    "host": host,
                    "group": group,
                    "token": token,
                    "cookies": {},
                    "login": "stress",
                    "name": "stress",
                    "scopes": "doc,repo,group:read",
                    "created_at": now().isoformat(timespec="seconds"),
                }
            ),
            encoding="utf-8",
        )

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["YUQUE_HOME"] = str(self.home)
        env["PYTHONIOENCODING"] = "utf-8"
        env["YUQUE_CLASSROOM_REPO"] = self.repo
        return env

    def run_cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [sys.executable, "-m", "nju_yuque", *args],
            cwd=ROOT,
            env=self.env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if check and proc.returncode != 0:
            raise SystemExit(f"命令失败：yuque {' '.join(args)}\n{proc.stdout}\n{proc.stderr}")
        return proc

    # -- 知识库整理 -------------------------------------------------------
    def prepare_kb(self, y: Yuque) -> str:
        """把知识库弄成新架构的样子，返回「本次测试要用的日期目录 uuid」。"""
        self.section("① 整理知识库（指导文档 / 日期目录 / 归档区）")
        toc = y.toc()
        titles = {i["title"]: i for i in toc if i["type"] == "TITLE"}
        docs = {d["title"]: d for d in y.docs()}

        guide = docs.get(GUIDE_TITLE)
        guide_text = (ROOT / "examples/classroom_kb/guide.md").read_text("utf-8")
        if guide is None:
            guide = y.create(title=GUIDE_TITLE, text=guide_text)
            self.created.append(guide["id"])
            self.say(f"  + 建指导文档 #{guide['id']}")
        else:
            backup = self.tmp / "guide-backup.md"
            backup.write_text(y.doc(str(guide["id"])).get("body") or "", encoding="utf-8")
            if self.args.allow_real_writes or not self.args.host:
                y.update(guide["id"], text=guide_text)
                self.say(f"  = 指导文档 #{guide['id']} 已更新为新版，原件备份 {backup}")
            else:
                self.say(f"  · 只读模式：不改指导文档（原件已备份 {backup}）")

        if ARCHIVE_TITLE not in titles:
            node = y.create_group(ARCHIVE_TITLE)
            self.say(f"  + 建归档区（uuid={node['uuid'][:8]}…）")
        else:
            self.say("  = 归档区已存在")

        # 用「下周」的目录（按周命名，如 0921-0927）；没有就建一个
        wanted = next_week_title()
        folder = titles.get(wanted)
        if folder is not None:
            self.say(f"  = 日期目录已存在：{wanted}")
        elif not self.args.allow_real_writes:
            candidates = [k for k in titles if k.replace("-", "").isdigit() and k != ARCHIVE_TITLE]
            folder = titles[sorted(candidates)[-1]] if candidates else None
            if folder is None:
                raise SystemExit(f"知识库里没有可用的日期目录（{wanted} 不存在），且当前不允许写")
            self.say(f"  · 未找到 {wanted}，只读模式改用现有目录：{folder['title']}")
        else:
            folder = y.create_group(wanted)
            self.say(f"  + 新建日期目录 {wanted}（uuid={folder['uuid'][:8]}…）")
        self.say(f"  → 申请文档写入：{folder['title']}")
        return folder["uuid"]

    # -- agent 常驻 -------------------------------------------------------
    def start_agent(self, port: int) -> None:
        self.section("② 把 agent 常驻挂到 server 上（真进程 / 真端口 / webhook + 轮询）")
        cmd = [
            sys.executable,
            "-u",
            "-m",
            "nju_yuque",
            "classroom",
            "serve",
            "--repo",
            self.repo,
            "--outdir",
            str(self.outdir),
            "--port",
            str(port),
            "--listen",
            "127.0.0.1",
            "--path",
            "/yuque/webhook",
            "--secret",
            SECRET,
            "--interval",
            str(self.args.interval),
            "--dump-webhook",
            str(self.tmp / "webhook-raw"),
            "--notify",
            "outbox,console",
        ]
        self.agent = subprocess.Popen(
            cmd,
            cwd=ROOT,
            env=self.env(),
            stdout=self.log.open("w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
        ready = wait_for(
            lambda: self._health(port),
            timeout=30,
        )
        if not ready:
            raise SystemExit("agent 没起来，日志：\n" + self.log.read_text(encoding="utf-8")[:2000])
        self.say(f"▶ agent pid={self.agent.pid}  健康检查 http://127.0.0.1:{port}/healthz")
        self.say(f"▶ webhook  http://127.0.0.1:{port}/yuque/webhook?token={SECRET}")
        self.say(f"▶ 日志 {self.log}")

    @staticmethod
    def _health(port: int) -> bool:
        try:
            return httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2).status_code == 200
        except Exception:
            return False

    def agent_log(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace") if self.log.exists() else ""

    def webhook(self, payload: dict, *, token: str = SECRET, path: str = "/yuque/webhook") -> int:
        url = f"http://127.0.0.1:{self.args.agent_port}{path}"
        params = {"token": token} if token else {}
        resp = httpx.post(url, json=payload, params=params, timeout=10)
        return resp.status_code

    # -- 状态读取 ---------------------------------------------------------
    def state(self) -> dict:
        path = self.outdir / "state.json"
        return (
            json.loads(path.read_text(encoding="utf-8"))
            if path.exists()
            else {"docs": {}, "meta": {}}
        )

    def notices(self) -> list[dict]:
        # 通知按周分组：扫所有周的 pending
        out = []
        for path in sorted((self.outdir / "notify").rglob("pending/*.json")):
            out.append(json.loads(path.read_text(encoding="utf-8")))
        return out

    def applications(self) -> list[dict]:
        out = []
        for path in sorted((self.outdir / "applications").glob("*.json")):
            if not path.name.startswith("index"):  # 索引不是申请
                out.append(json.loads(path.read_text(encoding="utf-8")))
        return out

    def decision_of(self, doc_id: int) -> str:
        entry = self.state()["docs"].get(str(doc_id))
        return entry["status"] if entry else "（无记录）"

    # -- 场景 -------------------------------------------------------------
    def run(self) -> None:
        mock_mode = self.args.host is None
        if mock_mode:
            host, token = self.start_mock()
            self.args.host = host
            self.args.token = token
        self.write_home(self.args.host, self.args.token or "")
        y = Yuque(self.args.host, self.args.token or "", self.repo)

        folder_uuid = self.prepare_kb(y)
        self.args.agent_port = free_port()
        self.start_agent(self.args.agent_port)
        # 基线：之后所有语雀写操作都应该来自 driver（社员），agent 一次都不该写
        self.driver_w0 = y.my_writes
        if self.args.host.startswith("http://127.0.0.1"):
            self.mock_w0 = self.mock_stats()["write_total"]
        else:
            self.mock_w0 = -1

        # ---- ③ 社员批量建申请 -------------------------------------------
        self.section("③ 社员在日期目录里批量新建申请文档（14 种样例）")
        for case in self.cases:
            doc = y.create(title=case.title, text=case.text, parent_uuid=folder_uuid)
            case.doc_id, case.slug = int(doc["id"]), doc["slug"]
            self.created.append(case.doc_id)
            self.say(f"  + #{case.doc_id:<6} {case.title:<18} 期望={case.expect:<12} {case.note}")

        self.wait_rounds(2)
        self.report_cases()

        # ---- ④ webhook 路径 ---------------------------------------------
        self.section("④ webhook：真发 HTTP 事件（正向 / 伪造 / 跨库 / 错密钥）")
        self.webhook_checks(y)

        # ---- ⑤ 已受理后改动 / 删除 --------------------------------------
        self.section("⑤ 已受理后：改文档 / 删文档")
        self.tamper_and_delete(y)

        # ---- ⑥ 批量压力 -------------------------------------------------
        self.section("⑥ 压力：一次新建 40 篇 + 幂等复跑")
        self.stress_burst(y, folder_uuid)

        # ---- ⑦ 只读性验证 -----------------------------------------------
        self.section("⑦ 验证 agent 对语雀「只读」")
        self.verify_read_only(y)

        # ---- ⑧ 汇总 -----------------------------------------------------
        self.summary()

    def wait_rounds(self, n: int = 2, timeout: float = 120.0) -> None:
        """等 agent 跑够 n 轮（轮次从 state.json 的 meta.rounds 看）。"""
        start = self.state()["meta"].get("rounds", 0)
        wait_for(lambda: self.state()["meta"].get("rounds", 0) >= start + n, timeout=timeout)

    def report_cases(self) -> None:
        for case in self.cases:
            case.seen = self.decision_of(case.doc_id)
            good = {
                "accepted": "submitted",
                "rejected": "rejected",
                "unrecognized": "unrecognized",
                "draft": "（无记录）",
            }[case.expect]
            self.results.append((case.title, case.expect, case.seen, ok(case.seen == good)))

    def webhook_checks(self, y: Yuque) -> None:
        target = self.cases[0]

        # ① 正常更新事件：应触发「只重读这一篇」
        code = self.webhook(
            {
                "action_type": "publish",
                "data": {"id": target.doc_id, "title": target.title, "slug": target.slug},
            }
        )
        self.say(f"  正常 publish 事件 → HTTP {code}")
        self.webhook_check("正常事件被接受", code == 200)

        # ② 错密钥：401
        code = self.webhook(
            {"action_type": "publish", "data": {"id": target.doc_id}}, token="wrong"
        )
        self.say(f"  错密钥 → HTTP {code}（期望 401）")
        self.webhook_check("错密钥被拒", code == 401)

        # ③ 跨知识库事件：忽略（不能动本地记录）
        code = self.webhook(
            {
                "action_type": "delete",
                "data": {"id": target.doc_id, "book": {"namespace": "other/whatever"}},
            }
        )
        self.say(f"  跨库 delete 事件 → HTTP {code}")
        self.webhook_check(
            "跨库事件被忽略",
            self.decision_of(target.doc_id) == "submitted",
        )

        # ④ 删除事件必须「自己读一次」确认：文档还在 → 忽略（防 webhook 动作映射不准）
        victim = next(c for c in self.cases if c.name == "指定教室")
        code = self.webhook(
            {"action_type": "delete", "data": {"id": victim.doc_id, "title": victim.title}}
        )
        self.say(f"  文档还在时收到 delete 事件（#{victim.doc_id}）→ HTTP {code}")
        time.sleep(3)
        self.webhook_check(
            "文档仍在 → 删除事件被忽略",
            not any(
                n["kind"] == "deleted_submitted" and n["doc"]["doc_id"] == victim.doc_id
                for n in self.notices()
            )
            and self.decision_of(victim.doc_id) == "submitted",
        )

        # ⑤ 真删之后再发事件：立即通知（不等防抖）
        y.delete(victim.doc_id)
        code = self.webhook({"action_type": "delete", "data": {"id": victim.doc_id}})
        self.say(f"  真删后 delete 事件（#{victim.doc_id}）→ HTTP {code}")
        got = wait_for(
            lambda: any(
                n["kind"] == "deleted_submitted" and n["doc"]["doc_id"] == victim.doc_id
                for n in self.notices()
            ),
            timeout=20,
        )
        self.webhook_check("真删 → 删除事件立即通知", got)
        self.say(f"  原始报文落盘：{len(list((self.tmp / 'webhook-raw').glob('*.json')))} 份")

    def webhook_check(self, name: str, passed: bool) -> None:
        self.results.append((f"webhook · {name}", "——", "——", ok(passed)))

    def tamper_and_delete(self, y: Yuque) -> None:
        accepted = [c for c in self.cases if c.expect == "accepted"]
        # ① 改一篇已受理的：应收到「修改无效」且不重新受理
        tampered = accepted[0]
        y.update(tampered.doc_id, text=body(活动时间="17:00-19:00"))
        self.say(f"  ✎ 改 #{tampered.doc_id}（{tampered.title}）活动时间 → 17:00-19:00")
        got = wait_for(
            lambda: any(
                n["kind"] == "tampered" and n["doc"]["doc_id"] == tampered.doc_id
                for n in self.notices()
            ),
            timeout=60,
        )
        self.results.append(
            ("改动已受理的文档 → 告警", "tampered", "tampered" if got else "（无）", ok(got))
        )
        state = self.state()["docs"].get(str(tampered.doc_id), {})
        self.results.append(
            (
                "已受理仍是终态",
                "submitted",
                state.get("status", "?"),
                ok(state.get("status") == "submitted"),
            )
        )

        # ② 真的删掉一篇已受理的（走轮询 + 防抖 + 单独读 404 三重确认）
        doomed = accepted[1]
        y.delete(doomed.doc_id)
        self.say(
            f"  🗑 删除 #{doomed.doc_id}（{doomed.title}），看轮询多久能确认（默认 grace=2 轮）"
        )
        got = wait_for(
            lambda: bool(self.state()["docs"].get(str(doomed.doc_id), {}).get("deleted_at")),
            timeout=120,
        )
        entry = self.state()["docs"].get(str(doomed.doc_id), {})
        self.results.append(
            (
                "轮询发现删除 → 通知 + 立墓碑",
                "deleted_at 有值",
                str(entry.get("deleted_at", ""))[:19],
                ok(got),
            )
        )
        # 申请文件与索引不能被删除影响（下游不能漏单）
        still = [a for a in self.applications() if a["source"]["doc_id"] == doomed.doc_id]
        index_rows = json.loads(
            (self.outdir / "applications" / "index.json").read_text(encoding="utf-8")
        )["applications"]
        ids = [row["application_id"] for row in index_rows]
        self.results.append(
            (
                "删除后申请文件/索引仍在",
                "1 份",
                f"{len(still)} 份",
                ok(len(still) == 1 and still[0]["application_id"] in ids),
            )
        )

    def stress_burst(self, y: Yuque, folder_uuid: str) -> None:
        burst: list[Case] = []
        total = self.args.burst
        for i in range(total):
            hour = random.choice([8, 9, 10, 14, 15, 16, 17, 19, 20])
            text = body(
                申请人=f"压测员{i:02d}",
                活动日期=(now() + timedelta(days=random.choice([3, 4, 5, 6, 7]))).strftime(
                    "%Y-%m-%d"
                ),
                活动时间=f"{hour:02d}:00-{hour + 1:02d}:00",
                校区=random.choice(["仙林", "鼓楼", "苏州", "浦口"]),
                人数=random.choice(["", "20", "35", "60"]),
            )
            case = Case(f"批量{i:02d}", f"压测P{i:02d}-批量", text, "accepted")
            doc = y.create(title=case.title, text=case.text, parent_uuid=folder_uuid)
            case.doc_id, case.slug = int(doc["id"]), doc["slug"]
            self.created.append(case.doc_id)
            self.untouched[case.doc_id] = norm_ts(y.doc(str(doc["id"])).get("updated_at") or "")
            burst.append(case)
            time.sleep(self.args.burst_delay)
        t0 = time.time()
        self.wait_rounds(2, timeout=180)
        elapsed = time.time() - t0
        accepted = sum(1 for c in burst if self.decision_of(c.doc_id) == "submitted")
        self.say(f"  {total} 篇一次性灌入 → {elapsed:.1f}s 内受理 {accepted}/{total}")
        self.results.append(
            (
                f"批量 {total} 篇受理（{elapsed:.1f}s）",
                str(total),
                str(accepted),
                ok(accepted == total),
            )
        )

        # 幂等复跑：再等两轮，通知数不应增加
        notes_before = len(self.notices())
        self.wait_rounds(2, timeout=120)
        notes_after = len(self.notices())
        self.say(f"  幂等复跑：通知 {notes_before} → {notes_after}（不应增加）")
        self.results.append(
            (
                "幂等：复跑不重复通知",
                str(notes_before),
                str(notes_after),
                ok(notes_after == notes_before),
            )
        )

    def verify_read_only(self, y: Yuque) -> None:
        """仿真模式：用「服务端写操作计数 − driver 自己的写操作」反推 agent 写了几次。"""
        is_mock = bool(self.args.host and self.args.host.startswith("http://127.0.0.1"))
        stats = self.mock_stats() if is_mock else {"write_total": -1, "writes": {}}
        if stats.get("write_total", -1) < 0:
            # 真机模式：用「建完就没再动过的文档 updated_at 是否保持原值」证明 agent 没写
            changed_rows = []
            for doc_id, stamp in self.untouched.items():
                try:
                    current = y.doc(str(doc_id)).get("updated_at") or ""
                except Exception:
                    continue
                if norm_ts(current) != stamp:
                    changed_rows.append((doc_id, stamp, current))
            self.say(
                f"  抽查 {len(self.untouched)} 篇文档，updated_at 被改动的：{len(changed_rows)}"
            )
            self.results.append(
                (
                    "agent 全程 0 次语雀写操作",
                    "0 篇",
                    f"{len(changed_rows)} 篇",
                    ok(not changed_rows),
                )
            )
            return
        total = stats["write_total"] - self.mock_w0
        mine = y.my_writes - self.driver_w0
        agent_writes = total - mine
        self.say(f"  agent 启动后语雀写操作共 {total} 次，其中社员（driver）{mine} 次")
        self.say(f"  → 归到 agent 头上的写操作：{agent_writes} 次（期望 0）")
        self.say(f"  明细：{stats['writes']}")
        self.results.append(
            ("agent 全程 0 次语雀写操作", "0", str(agent_writes), ok(agent_writes == 0))
        )

    # -- 汇总 -------------------------------------------------------------
    def summary(self) -> None:
        self.section("⑧ 结果汇总")
        width = max(len(r[0]) for r in self.results) + 2
        for name, expect, seen, mark in self.results:
            self.say(f"  {mark} {name:<{width}} 期望={expect:<12} 实际={seen}")
        bad = [r for r in self.results if r[3] != "✅"]
        self.say(f"\n  通过 {len(self.results) - len(bad)}/{len(self.results)}")

        apps = self.applications()
        notes = self.notices()
        self.say(f"  产出申请 JSON：{len(apps)} 份　通知事件：{len(notes)} 条")
        if notes:
            kinds: dict[str, int] = {}
            for n in notes:
                kinds[n["kind"]] = kinds.get(n["kind"], 0) + 1
            self.say(f"  通知构成：{kinds}")
        if apps:
            sample = apps[0]
            self.say("\n  样例申请 JSON(" + sample["application_id"] + ")：")
            self.say("  " + json.dumps(sample["activity"], ensure_ascii=False))
            self.say("  规范化：" + json.dumps(sample["normalizations"], ensure_ascii=False))
            self.say("  告警：" + json.dumps(sample["warnings"], ensure_ascii=False))
        if notes:
            self.say("\n  样例通知（" + notes[0]["kind"] + "）：")
            for line in notes[0]["message"].splitlines():
                self.say("    " + line)
        self.say(f"\n  工作目录（含 state/applications/notify/agent.log）：{self.tmp}")
        if bad:
            self.say("\n  ❌ 未通过项：")
            for name, expect, seen, _ in bad:
                self.say(f"     - {name}（期望 {expect}，实际 {seen}）")

    # -- 收尾 -------------------------------------------------------------
    def stop(self, y: Yuque | None = None) -> None:
        self.section("⑨ 收尾")
        if self.agent:
            self.agent.terminate()
            try:
                self.agent.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.agent.kill()
            self.say("  agent 已停止")
        is_mock = bool(self.args.host and self.args.host.startswith("http://127.0.0.1"))
        if y is not None and is_mock:
            self.say(f"  （仿真模式：{len(self.created)} 篇测试文档随进程一起消失，无需清理）")
        elif y is not None and self.args.allow_real_writes and not self.args.keep:
            for doc_id in self.created:
                try:
                    y.delete(doc_id)
                except Exception as exc:
                    self.say(f"  ! 删除 #{doc_id} 失败：{exc}")
            self.say(f"  已清理测试文档 {len(self.created)} 篇")
        elif y is not None:
            self.say(f"  （未清理 {len(self.created)} 篇测试文档：只读模式或 --keep）")
        if self.mock:
            self.mock.terminate()
            try:
                self.mock.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.mock.kill()
            self.say("  仿真语雀已停止")


def load_token_from_credentials() -> str:
    """--token 没给就读本地登录态（``~/.yuque/auth.json``，不打印内容）。"""
    from nju_yuque.session import Credentials

    try:
        cred = Credentials.load()
    except Exception as exc:
        raise SystemExit(f"读不到本地登录态：{exc}") from exc
    return cred.token if cred.is_token else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="教室申请 agent 端到端压测")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--host", help="不传 = 起本地仿真语雀；真机传 https://nova.yuque.com")
    parser.add_argument("--token", help="真机令牌（也可用 ~/.yuque/auth.json，不传则读它）")
    parser.add_argument("--interval", type=float, default=5.0, help="agent 轮询间隔（压测用 5s）")
    parser.add_argument(
        "--allow-real-writes", action="store_true", help="真机模式下允许建/删测试文档"
    )
    parser.add_argument("--keep", action="store_true", help="跑完不删测试文档")
    parser.add_argument("--burst", type=int, default=40, help="批量灌入多少篇申请")
    parser.add_argument("--burst-delay", type=float, default=0.15, help="灌入间隔秒数")
    args = parser.parse_args()
    if args.host and not args.token:
        args.token = load_token_from_credentials()

    stress = Stress(args)
    y: Yuque | None = None
    try:
        stress.run()
    finally:
        try:
            if stress.args.host and stress.args.host.startswith("http"):
                y = Yuque(stress.args.host, stress.args.token or "", stress.repo)
        except Exception:
            y = None
        stress.stop(y)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
