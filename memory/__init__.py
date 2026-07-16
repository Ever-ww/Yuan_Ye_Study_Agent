"""长期记忆与学习资料库的公共入口。

``SQLiteMemoryStore`` 管理可审计、可遗忘的用户/项目事实；``CorpusStore`` 管理
带文件与页码引用的学习资料。两者故意不提供统一 ``search`` 聚合，以免调用者
混淆“模型记住的事实”和“可验证的资料来源”。
"""

from .store import CorpusStore, SQLiteMemoryStore

__all__ = ["CorpusStore", "SQLiteMemoryStore"]
