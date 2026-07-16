"""提供可审计的长期记忆，以及与记忆严格分离的学习资料库。

两类数据共享 :class:`Agent.storage.StateStore` 的 SQLite 连接，但使用不同表和
FTS5 索引：长期记忆保存“关于用户或项目的事实”，资料库保存可引用的原文分块。
这种物理/接口分离能避免模型把个人偏好误当外部证据，也方便用户独立删除记忆
而不破坏论文索引。``MEMORY.md`` 只是便于人工检查的派生索引，SQLite 才是事实
来源；资料库则通过文件哈希实现幂等更新，并保留 PDF 页码用于回答引用。
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from Agent.storage import StateStore
from Agent.types import utc_now


def _fts_query(query: str) -> str:
    """把自由文本转换为保守的 SQLite FTS5 MATCH 表达式。

    仅保留 Unicode 单词字符和连续中文字符，将最多 20 个 token 分别加双引号后
    用 OR 连接。这样既避免用户输入的 FTS 运算符改变查询语义，也控制超长查询
    的解析成本。空查询返回一个不会引入裸语法的空短语。
    """

    tokens = re.findall(r"[\w\u4e00-\u9fff]+", query, re.UNICODE)
    return " OR ".join(f'"{token}"' for token in tokens[:20]) or '""'


class SQLiteMemoryStore:
    """基于 SQLite/FTS5 的长期记忆仓库。

    每条记忆带作用域、来源、置信度、创建/更新时间和 ``active`` 状态。删除采用
    软删除：结构化记录仍留在数据库供审计，全文索引项立即移除，因此正常列表
    与召回不会再看到它。写操作后重建 ``MEMORY.md`` 人工索引，便于用户在不
    直接操作数据库的情况下检查当前有效内容。
    """

    def __init__(self, store: StateStore, memory_dir: Path, default_scope: str | None = None) -> None:
        """绑定状态库和索引目录，并确保目录存在。

        ``default_scope`` 可由运行时设为项目或用户作用域；每个方法的显式 scope
        始终优先。构造函数不会自动扫描或写入任何记忆。
        """

        self.store = store
        self.memory_dir = memory_dir
        self.default_scope = default_scope
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    def add(self, content: str, *, scope: str | None = None, source: str = "user", confidence: float = 1.0) -> str:
        """新增一条记忆，或返回同作用域内完全相同的现有记忆 ID。

        去重基于 ``strip`` 后的精确文本和作用域，避免自动改写大小写或标点后把
        含义不同的事实错误合并。置信度被限制在 0..1。空内容被拒绝；成功插入
        时同步写入 FTS 表和人工索引。

        Returns:
            新建或去重命中的记忆 ID。
        """

        scope = scope or self.default_scope or "project"
        normalized = content.strip()
        if not normalized:
            raise ValueError("记忆不能为空")
        duplicate = self.store.query("SELECT id FROM memories WHERE active=1 AND scope=? AND content=?", (scope, normalized))
        if duplicate:
            return str(duplicate[0]["id"])
        memory_id, now = uuid4().hex, utc_now()
        self.store.execute(
            "INSERT INTO memories VALUES(?,?,?,?,?,?,?,?)",
            (memory_id, scope, normalized, source, max(0, min(1, confidence)), 1, now, now),
        )
        self.store.execute("INSERT INTO memory_fts(id,content) VALUES(?,?)", (memory_id, normalized))
        self._write_index()
        return memory_id

    def search(self, query: str, *, scope: str | None = None, limit: int = 8) -> list[dict[str, Any]]:
        """按全文相关度召回有效记忆，并在 FTS 不可用时降级为 LIKE。

        默认使用 FTS5 ``bm25`` 排序，相同相关度再按更新时间倒序。scope 可限制
        召回边界，limit 被夹在 1..50 之间以控制 Prompt 体积。MATCH、bm25 或索引
        查询在执行阶段异常时，会执行字面 ``LIKE`` 查询；降级结果不具备分词/
        相关度排序能力。该降级无法补救 ``StateStore`` 初始化时就缺少 FTS5 的
        情况，因为虚拟表在更早阶段创建。
        """

        scope = scope or self.default_scope
        where, params = "m.active=1", []
        if scope:
            where += " AND m.scope=?"
            params.append(scope)
        params = [_fts_query(query), *params, min(50, max(1, limit))]
        try:
            return self.store.query(
                f"SELECT m.*, bm25(memory_fts) AS rank FROM memory_fts JOIN memories m ON m.id=memory_fts.id WHERE memory_fts MATCH ? AND {where} ORDER BY rank, m.updated_at DESC LIMIT ?",
                tuple(params),
            )
        except Exception:
            like = f"%{query}%"
            return self.store.query(f"SELECT * FROM memories m WHERE {where} AND content LIKE ? ORDER BY updated_at DESC LIMIT ?", (*params[1:-1], like, params[-1]))

    def list(self, scope: str | None = None) -> list[dict[str, Any]]:
        """按更新时间倒序列出指定作用域内的全部有效记忆。"""

        scope = scope or self.default_scope
        if scope:
            return self.store.query("SELECT * FROM memories WHERE active=1 AND scope=? ORDER BY updated_at DESC", (scope,))
        return self.store.query("SELECT * FROM memories WHERE active=1 ORDER BY updated_at DESC")

    def forget(self, memory_id: str) -> bool:
        """软删除记忆并从全文索引移除。

        返回 ``False`` 表示 ID 不存在或此前已遗忘。结构化行保留并更新时间，
        从而能够审计删除动作；当前 API 不自动恢复，显式 ``edit`` 可重新激活。
        """

        rows = self.store.query("SELECT id FROM memories WHERE id=? AND active=1", (memory_id,))
        if not rows:
            return False
        self.store.execute("UPDATE memories SET active=0,updated_at=? WHERE id=?", (utc_now(), memory_id))
        self.store.execute("DELETE FROM memory_fts WHERE id=?", (memory_id,))
        self._write_index()
        return True

    def get(self, memory_id: str) -> dict[str, Any] | None:
        """按 ID 获取结构化记录，包括已经软删除的记忆。"""

        rows = self.store.query("SELECT * FROM memories WHERE id=?", (memory_id,))
        return rows[0] if rows else None

    def edit(self, memory_id: str, content: str) -> bool:
        """替换记忆正文、重新激活记录，并同步刷新 FTS 条目。

        空内容被拒绝；未知 ID 返回 ``False``。这里先删除旧索引再插入新索引，
        确保同一 ID 不会在搜索中保留过期文本。底层 ``StateStore.execute`` 会
        分别提交各条 SQL，因此这不是跨语句事务；进程在步骤之间异常退出时，
        结构化记录和 FTS 可能暂时不一致，需要重新编辑以修复。
        """

        normalized = content.strip()
        if not normalized:
            raise ValueError("记忆不能为空")
        if not self.get(memory_id):
            return False
        self.store.execute("UPDATE memories SET content=?,active=1,updated_at=? WHERE id=?", (normalized, utc_now(), memory_id))
        self.store.execute("DELETE FROM memory_fts WHERE id=?", (memory_id,))
        self.store.execute("INSERT INTO memory_fts(id,content) VALUES(?,?)", (memory_id, normalized))
        self._write_index()
        return True

    def export(self) -> str:
        """将当前作用域的有效记忆导出为易读的 UTF-8 JSON 文本。"""

        return json.dumps(self.list(), ensure_ascii=False, indent=2)

    def index_text(self, max_lines: int = 200, max_bytes: int = 25_000) -> str:
        """读取受尺寸限制的 ``MEMORY.md``，供 System Prompt 注入。

        同时限制行数和 UTF-8 字节数，防止记忆索引无限增长挤占上下文。字节截断
        可能落在多字节字符中，因此使用 ``errors='ignore'`` 丢弃不完整尾字符。
        文件不存在时返回空串，而不是主动生成文件。
        """

        path = self.memory_dir / "MEMORY.md"
        if not path.exists():
            return ""
        lines = path.read_text(encoding="utf-8").splitlines()[:max_lines]
        return "\n".join(lines).encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")

    def _write_index(self) -> None:
        """从 SQLite 重建最多 200 条有效记忆的可审计 Markdown 索引。

        该文件是派生视图，不应被当作数据库的双向同步源。HTML 注释保留 ID 与
        来源，正文保持人类可读；编辑和删除仍应通过 CLI/API，以同步 FTS 状态。
        """

        memories = self.list()[:200]
        lines = ["# Yuan Ye Agent Memory", "", "> Auto-generated index. Use the CLI to edit or forget entries.", ""]
        lines += [f"- [{item['scope']}] {item['content']} <!-- {item['id']} source={item['source']} -->" for item in memories]
        (self.memory_dir / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


class CorpusStore:
    """对本地学习资料建立可引用的独立全文索引。

    支持 Markdown、纯文本、HTML 和 PDF。每个文档记录绝对路径与 SHA-256；
    每个分块记录文档 ID、可选页码/章节和正文。PDF 页码从 1 开始，对应用户
    阅读器中的自然页序，可直接用于回答引用。资料内容不会写入用户长期记忆。
    """

    SUPPORTED = {".md", ".txt", ".html", ".htm", ".pdf"}

    def __init__(self, store: StateStore) -> None:
        """绑定状态库；构造时不扫描文件系统。"""

        self.store = store

    def index_path(self, path: Path) -> dict[str, int]:
        """索引单个文件或递归索引目录中的受支持文件。

        首先对原始字节计算 SHA-256。相同路径且哈希未变化时直接跳过；路径相同
        但内容变化时，先清理旧文档的 FTS 分块及文档行，再写入新版本，避免同一
        文件的旧内容仍被召回。返回值只统计本次实际更新的文档与新建分块数。

        路径不存在时当前实现会按空目录处理；文件读取、PDF 解析或数据库错误会
        原样抛出并交由 CLI 展示。各 SQL 当前分别提交而非包在一个大事务中，
        所以中途失败可能已删除旧索引或写入部分新分块，调用者应重新索引该文件。
        """

        files = [path] if path.is_file() else [item for item in path.rglob("*") if item.suffix.lower() in self.SUPPORTED]
        documents = chunks = 0
        for file in files:
            if file.suffix.lower() not in self.SUPPORTED:
                continue
            data = file.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            existing = self.store.query("SELECT id FROM corpus_documents WHERE path=? AND file_hash=?", (str(file.resolve()), digest))
            if existing:
                continue
            old = self.store.query("SELECT id FROM corpus_documents WHERE path=?", (str(file.resolve()),))
            for row in old:
                ids = self.store.query("SELECT id FROM corpus_chunks WHERE document_id=?", (row["id"],))
                for chunk in ids:
                    self.store.execute("DELETE FROM corpus_fts WHERE id=?", (chunk["id"],))
                self.store.execute("DELETE FROM corpus_documents WHERE id=?", (row["id"],))
            document_id = uuid4().hex
            extracted = self._extract(file)
            self.store.execute("INSERT INTO corpus_documents VALUES(?,?,?,?,?)", (document_id, str(file.resolve()), digest, file.stem, utc_now()))
            for page, section, text in extracted:
                for chunk_text in self._chunk(text):
                    chunk_id = uuid4().hex
                    self.store.execute("INSERT INTO corpus_chunks VALUES(?,?,?,?,?)", (chunk_id, document_id, page, section, chunk_text))
                    self.store.execute("INSERT INTO corpus_fts(id,content) VALUES(?,?)", (chunk_id, chunk_text))
                    chunks += 1
            documents += 1
        return {"documents": documents, "chunks": chunks}

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """按 BM25 相关度搜索资料分块，并返回文件路径与页码等引用字段。

        limit 被限制在 1..30。与记忆搜索不同，此处不做 LIKE 降级，因为资料回答
        依赖稳定的分块和排名；若运行环境没有 FTS5，应显式修复索引能力，而不是
        静默返回可能误导引用的结果。
        """

        return self.store.query(
            "SELECT d.path,d.title,c.page,c.section,c.content,bm25(corpus_fts) AS rank FROM corpus_fts JOIN corpus_chunks c ON c.id=corpus_fts.id JOIN corpus_documents d ON d.id=c.document_id WHERE corpus_fts MATCH ? ORDER BY rank LIMIT ?",
            (_fts_query(query), min(30, max(1, limit))),
        )

    @staticmethod
    def _extract(path: Path) -> list[tuple[int | None, str | None, str]]:
        """抽取文件文本，并返回 ``(页码, 章节, 正文)`` 列表。

        PDF 使用可选 ``pypdf`` 逐页提取，页码从 1 开始；缺少依赖时给出明确错误。
        文本类文件按 UTF-8 读取并替换坏字节。HTML 会先去除 script/style，再
        去标签和解码实体；这是轻量抽取，不执行脚本，也不访问外部资源。当前
        Markdown/HTML 尚未解析章节标题，因此 section 为 ``None``。
        """

        suffix = path.suffix.lower()
        if suffix == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError as exc:
                raise RuntimeError("索引 PDF 需要安装 pypdf") from exc
            reader = PdfReader(str(path))
            return [(index, None, page.extract_text() or "") for index, page in enumerate(reader.pages, start=1)]
        text = path.read_text(encoding="utf-8", errors="replace")
        if suffix in {".html", ".htm"}:
            text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", text)
            text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", text))
        return [(None, None, text)]

    @staticmethod
    def _chunk(text: str, size: int = 1800, overlap: int = 200) -> list[str]:
        """把规范化文本切为带重叠的定长字符块。

        连续空白先折叠为一个空格，以减少无意义 token。默认每块 1800 字符、相邻
        重叠 200 字符，使跨边界句子仍有机会完整命中。这里按字符而非 tokenizer
        切分，因此大小只是与模型无关的近似值；空文本不产生分块。
        """

        clean = re.sub(r"\s+", " ", text).strip()
        if not clean:
            return []
        return [clean[start:start + size] for start in range(0, len(clean), size - overlap)]
