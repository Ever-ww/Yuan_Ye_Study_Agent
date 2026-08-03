"""下载公开论文 PDF 到工作区，并纳入文件锁与 checkpoint 事务。"""

from __future__ import annotations

import hashlib
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict

from tool.contracts import ToolContext
from tool.path_guard import safe_workspace_path
from tools.web_fetch import (
    HostResolver,
    WebFetchNetworkError,
    WebFetchSecurityError,
    WebFetchServiceError,
    WebFetchTool,
    _REDIRECT_STATUSES,
    _content_type,
    _read_limited,
)


_PDF_CONTENT_TYPES = {"application/pdf", "application/octet-stream"}


class PaperDownloadResponse(BaseModel):
    """返回给模型的稳定下载结果，可直接衔接 `read_file`。"""

    model_config = ConfigDict(frozen=True, strict=True)

    requested_url: str
    final_url: str
    path: str
    content_type: str
    bytes_written: int
    sha256: str
    checkpoint: str | None = None
    next_tool: Literal["read_file"] = "read_file"


class PaperDownloadSecurityError(RuntimeError):
    """下载目标、内容或工作区路径不符合安全边界。"""


class PaperDownloadNetworkError(RuntimeError):
    """论文下载连接或传输失败。"""


class PaperDownloadServiceError(RuntimeError):
    """论文站点返回不可接受的状态或内容。"""


class PaperDownloadTool(WebFetchTool):
    """只下载公开 HTTP(S) PDF，不执行内容，也不访问本机或内网。"""

    name = "download_paper"
    description = (
        "将 ArXiv、出版社或其他公开 HTTP(S) 地址的论文 PDF 下载到当前工作区；"
        "仅接受真实 PDF，并在成功后返回可交给 read_file 的相对路径"
    )
    risk = "write"
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "minLength": 1, "maxLength": 4000},
            "path": {
                "type": "string",
                "description": "工作区内以 .pdf 结尾的保存路径，例如 papers/attention.pdf",
            },
            "overwrite": {"type": "boolean"},
        },
        "required": ["url", "path"],
    }

    def __init__(
        self,
        *,
        timeout_seconds: int = 60,
        max_bytes: int = 50_000_000,
        max_redirects: int = 5,
        use_system_proxy: bool = False,
        proxy_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: HostResolver | None = None,
    ) -> None:
        super().__init__(
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            max_chars=1000,
            max_redirects=max_redirects,
            use_system_proxy=use_system_proxy,
            proxy_url=proxy_url,
            transport=transport,
            resolver=resolver,
        )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if context.sandbox is None:
            raise RuntimeError("当前 Runtime 未启用 checkpoint，禁止下载论文")
        if context.file_locks is None:
            raise RuntimeError("当前 Runtime 未启用文件锁，禁止下载论文")

        requested_url = str(arguments["url"]).strip()
        relative_path = str(arguments["path"])
        path = safe_workspace_path(context.project_root, relative_path)
        if path.suffix.lower() != ".pdf":
            raise PaperDownloadSecurityError("download_paper.path 必须以 .pdf 结尾")
        if path.exists() and not path.is_file():
            raise PaperDownloadSecurityError(f"目标路径不是普通文件：{relative_path}")
        overwrite = bool(arguments.get("overwrite", False))

        async with context.file_locks.write(path):
            if path.exists() and not overwrite:
                raise FileExistsError(
                    f"目标 PDF 已存在：{relative_path}；如需替换请显式设置 overwrite=true",
                )
            body, final_url, content_type = await self.download_bytes(requested_url)
            if path.is_file() and path.read_bytes() == body:
                return PaperDownloadResponse(
                    requested_url=requested_url,
                    final_url=final_url,
                    path=relative_path,
                    content_type=content_type,
                    bytes_written=len(body),
                    sha256=hashlib.sha256(body).hexdigest(),
                ).model_dump_json()

            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            try:
                temporary.write_bytes(body)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)

            try:
                checkpoint = await context.sandbox.checkpoint_write(relative_path)
            except Exception:
                await context.sandbox.restore_current()
                raise

            return PaperDownloadResponse(
                requested_url=requested_url,
                final_url=final_url,
                path=relative_path,
                content_type=content_type,
                bytes_written=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
                checkpoint=checkpoint.commit_sha if checkpoint is not None else None,
            ).model_dump_json()

    async def download_bytes(self, requested_url: str) -> tuple[bytes, str, str]:
        """下载并验证公开 PDF，但不决定保存位置。

        workspace 下载工具和全局论文库共用这一安全边界，避免两套 URL、
        重定向、代理、MIME 与 PDF 文件头校验逐渐产生差异。
        """
        requested = requested_url.strip()
        if not requested:
            raise PaperDownloadSecurityError("论文下载 URL 不能为空")
        current_url = await self._paper_url(_normalize_known_paper_url(requested))
        options: dict[str, Any] = {
            "timeout": httpx.Timeout(self.timeout_seconds, connect=min(15, self.timeout_seconds)),
            "trust_env": bool(self.use_system_proxy and not self.proxy_url),
            "follow_redirects": False,
        }
        if self.proxy_url:
            options["proxy"] = self.proxy_url
        if self._transport is not None:
            options["transport"] = self._transport
        return await self._download(requested, current_url, options)

    async def _download(
        self,
        requested_url: str,
        current_url: str,
        options: dict[str, Any],
    ) -> tuple[bytes, str, str]:
        try:
            async with httpx.AsyncClient(**options) as client:
                for redirect_count in range(self.max_redirects + 1):
                    async with client.stream(
                        "GET",
                        current_url,
                        headers={
                            "Accept": "application/pdf, application/octet-stream;q=0.8",
                            "User-Agent": "YuanYeAgent/1.0 (+local controlled paper download)",
                        },
                    ) as response:
                        self._validate_connected_peer(response)
                        if response.status_code in _REDIRECT_STATUSES:
                            location = response.headers.get("location")
                            if not location:
                                raise PaperDownloadServiceError("论文站点返回重定向但缺少 Location")
                            if redirect_count >= self.max_redirects:
                                raise PaperDownloadServiceError("论文下载重定向次数超过限制")
                            current_url = await self._paper_url(urljoin(current_url, location))
                            continue
                        if response.status_code < 200 or response.status_code >= 300:
                            raise PaperDownloadServiceError(
                                f"论文下载返回 HTTP {response.status_code}",
                            )
                        content_type = _content_type(response.headers.get("content-type"))
                        if content_type not in _PDF_CONTENT_TYPES:
                            raise PaperDownloadServiceError(
                                f"下载地址未返回 PDF：{content_type or 'missing'}",
                            )
                        try:
                            body = await _read_limited(response, self.max_bytes)
                        except WebFetchServiceError as exc:
                            raise PaperDownloadServiceError(
                                str(exc).replace("网页响应", "论文文件"),
                            ) from exc
                        if not body.lstrip().startswith(b"%PDF-"):
                            raise PaperDownloadSecurityError("响应内容不是有效的 PDF 文件头")
                        return body, current_url, content_type
        except (PaperDownloadSecurityError, PaperDownloadServiceError):
            raise
        except WebFetchSecurityError as exc:
            raise PaperDownloadSecurityError(
                str(exc).replace("web_fetch", "download_paper"),
            ) from exc
        except (WebFetchNetworkError, httpx.HTTPError) as exc:
            raise PaperDownloadNetworkError(
                f"论文下载失败（{type(exc).__name__}）；请检查网络与代理配置",
            ) from exc
        raise PaperDownloadServiceError(f"论文下载未产生有效响应：{requested_url}")

    async def _paper_url(self, value: str) -> str:
        try:
            return await self._validated_url(value)
        except WebFetchSecurityError as exc:
            raise PaperDownloadSecurityError(
                str(exc).replace("web_fetch", "download_paper"),
            ) from exc
        except WebFetchNetworkError as exc:
            raise PaperDownloadNetworkError(
                str(exc).replace("web_fetch", "download_paper"),
            ) from exc


def _normalize_known_paper_url(value: str) -> str:
    """把 ArXiv 摘要页转换为官方 PDF 地址；其他站点保持原样。"""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    host = (parsed.hostname or "").rstrip(".").lower()
    if host in {"arxiv.org", "www.arxiv.org"} and parsed.path.startswith("/abs/"):
        return urlunsplit((
            parsed.scheme,
            parsed.netloc,
            "/pdf/" + parsed.path[len("/abs/"):],
            "",
            "",
        ))
    return value
