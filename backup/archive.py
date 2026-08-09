"""Streaming ZIP64 + AES-256-GCM Agent Home archive."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .catalog import AgentHomeDurabilityCatalog, UnsafeArchiveEntryError
from .models import BackupFileRecord, BackupManifest


MAGIC = b"YYBACKUP\x01"
TAG_BYTES = 16
MAX_HEADER_BYTES = 16 * 1024
SALT_BYTES = 16
NONCE_BYTES = 12
KEY_BYTES = 32
DEFAULT_SCRYPT_N = 1 << 15
MAX_SCRYPT_N = 1 << 18
MAX_SCRYPT_R = 16
MAX_SCRYPT_P = 4
MAX_KDF_MEMORY_BYTES = 512 * 1024 * 1024


class ArchiveHeader(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format_version: int = 1
    algorithm: str = "AES-256-GCM"
    kdf: str = "scrypt"
    scrypt_n: int = Field(default=DEFAULT_SCRYPT_N)
    scrypt_r: int = Field(default=8)
    scrypt_p: int = Field(default=1)
    key_length: int = KEY_BYTES
    salt: str
    nonce: str


@dataclass(frozen=True)
class ArchiveSource:
    source: Path
    archive_path: str
    record: BackupFileRecord


class _EncryptingWriter:
    def __init__(self, raw: BinaryIO, encryptor) -> None:
        self.raw = raw
        self.encryptor = encryptor
        self.position = 0

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def write(self, data: bytes) -> int:
        if not data:
            return 0
        self.raw.write(self.encryptor.update(data))
        self.position += len(data)
        return len(data)

    def tell(self) -> int:
        return self.position

    def seek(self, *_args) -> int:
        raise OSError("encrypted backup stream is not seekable")

    def flush(self) -> None:
        self.raw.flush()


def _crypto():
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError as exc:  # pragma: no cover - dependency packaging failure
        raise RuntimeError("Backup 需要安装 cryptography") from exc
    return Cipher, algorithms, modes, Scrypt


def _derive_key(passphrase: str, header: ArchiveHeader) -> bytes:
    if not passphrase:
        raise ValueError("Backup 口令不能为空")
    _, _, _, Scrypt = _crypto()
    salt = base64.b64decode(header.salt, validate=True)
    kdf = Scrypt(
        salt=salt,
        length=header.key_length,
        n=header.scrypt_n,
        r=header.scrypt_r,
        p=header.scrypt_p,
    )
    return kdf.derive(passphrase.encode("utf-8"))


def _validate_header(raw: bytes) -> ArchiveHeader:
    if not raw or len(raw) > MAX_HEADER_BYTES:
        raise ValueError("Backup Header 长度无效")
    header = ArchiveHeader.model_validate_json(raw, strict=True)
    if header.format_version != 1 or header.algorithm != "AES-256-GCM" or header.kdf != "scrypt":
        raise ValueError("Backup Header 版本、算法或KDF不受支持")
    try:
        salt = base64.b64decode(header.salt, validate=True)
        nonce = base64.b64decode(header.nonce, validate=True)
    except Exception as exc:
        raise ValueError("Backup Header Salt/Nonce 编码无效") from exc
    if len(salt) != SALT_BYTES or len(nonce) != NONCE_BYTES or header.key_length != KEY_BYTES:
        raise ValueError("Backup Header Salt、Nonce或Key长度无效")
    if (
        header.scrypt_n < (1 << 14)
        or header.scrypt_n > MAX_SCRYPT_N
        or header.scrypt_n & (header.scrypt_n - 1)
        or not 1 <= header.scrypt_r <= MAX_SCRYPT_R
        or not 1 <= header.scrypt_p <= MAX_SCRYPT_P
    ):
        raise ValueError("Backup Header scrypt参数超出安全范围")
    estimated = 128 * header.scrypt_n * header.scrypt_r * header.scrypt_p
    if estimated > MAX_KDF_MEMORY_BYTES:
        raise ValueError("Backup Header scrypt内存需求超出限制")
    return header


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_with_retry(source: Path, target: Path) -> None:
    last: OSError | None = None
    for attempt in range(6):
        try:
            os.replace(source, target)
            _fsync_directory(target.parent)
            return
        except OSError as exc:
            last = exc
            if os.name != "nt" or attempt == 5:
                raise
            time.sleep(0.05 * (2 ** attempt))
    assert last is not None
    raise last


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


class EncryptedBackupArchive:
    @classmethod
    def write(
        cls,
        target: Path,
        passphrase: str,
        manifest: BackupManifest,
        sources: Iterable[ArchiveSource],
    ) -> Path:
        Cipher, algorithms, modes, _ = _crypto()
        target = target.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.suffix != ".yybackup":
            target = target.with_suffix(".yybackup")
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.partial")
        salt, nonce = secrets.token_bytes(SALT_BYTES), secrets.token_bytes(NONCE_BYTES)
        header = ArchiveHeader(
            salt=base64.b64encode(salt).decode("ascii"),
            nonce=base64.b64encode(nonce).decode("ascii"),
        )
        header_bytes = header.model_dump_json().encode("utf-8")
        aad = MAGIC + len(header_bytes).to_bytes(4, "big") + header_bytes
        key = _derive_key(passphrase, header)
        try:
            with temporary.open("xb") as raw:
                raw.write(aad)
                encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
                encryptor.authenticate_additional_data(aad)
                writer = _EncryptingWriter(raw, encryptor)
                with zipfile.ZipFile(
                    writer,
                    mode="w",
                    compression=zipfile.ZIP_DEFLATED,
                    allowZip64=True,
                    compresslevel=6,
                ) as archive:
                    archive.writestr("manifest.json", manifest.model_dump_json(indent=2))
                    for source in sources:
                        info = zipfile.ZipInfo.from_file(source.source, arcname=source.archive_path)
                        info.compress_type = zipfile.ZIP_DEFLATED
                        info.create_system = 0
                        info.external_attr = 0
                        with source.source.open("rb") as incoming, archive.open(
                            info, "w", force_zip64=True,
                        ) as outgoing:
                            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
                raw.write(encryptor.finalize())
                raw.write(encryptor.tag)
                raw.flush()
                os.fsync(raw.fileno())
            _replace_with_retry(temporary, target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return target

    @classmethod
    def decrypt_to_zip(cls, archive: Path, passphrase: str, destination: Path) -> ArchiveHeader:
        Cipher, algorithms, modes, _ = _crypto()
        archive = archive.resolve()
        destination = destination.resolve()
        total_size = archive.stat().st_size
        with archive.open("rb") as source:
            if source.read(len(MAGIC)) != MAGIC:
                raise ValueError("不是有效的 Yuan Ye Backup")
            length_bytes = source.read(4)
            if len(length_bytes) != 4:
                raise ValueError("Backup Header 被截断")
            header_length = int.from_bytes(length_bytes, "big")
            if not 1 <= header_length <= MAX_HEADER_BYTES:
                raise ValueError("Backup Header 长度超出安全限制")
            raw_header = source.read(header_length)
            header = _validate_header(raw_header)
            aad = MAGIC + length_bytes + raw_header
            ciphertext_start = len(aad)
            ciphertext_length = total_size - ciphertext_start - TAG_BYTES
            if ciphertext_length <= 0:
                raise ValueError("Backup 加密数据被截断")
            source.seek(total_size - TAG_BYTES)
            tag = source.read(TAG_BYTES)
            source.seek(ciphertext_start)
            nonce = base64.b64decode(header.nonce)
            key = _derive_key(passphrase, header)
            decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
            decryptor.authenticate_additional_data(aad)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as output:
                remaining = ciphertext_length
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("Backup 加密数据被截断")
                    remaining -= len(chunk)
                    output.write(decryptor.update(chunk))
                output.write(decryptor.finalize())
                output.flush()
                os.fsync(output.fileno())
        return header

    @classmethod
    def inspect_manifest(cls, archive: Path, passphrase: str) -> BackupManifest:
        with tempfile.TemporaryDirectory(prefix="yy-backup-inspect-") as directory:
            zip_path = Path(directory) / "archive.zip"
            cls.decrypt_to_zip(archive, passphrase, zip_path)
            with zipfile.ZipFile(zip_path) as bundle:
                return BackupManifest.model_validate_json(bundle.read("manifest.json"), strict=True)

    @classmethod
    def extract(cls, archive: Path, passphrase: str, destination: Path) -> BackupManifest:
        destination = destination.resolve()
        destination.mkdir(parents=True, exist_ok=False)
        zip_path = destination.parent / f".{destination.name}.decrypted.{uuid4().hex}.zip"
        try:
            cls.decrypt_to_zip(archive, passphrase, zip_path)
            with zipfile.ZipFile(zip_path) as bundle:
                manifest = BackupManifest.model_validate_json(bundle.read("manifest.json"), strict=True)
                expected = {item.path: item for item in manifest.files}
                seen: set[str] = set()
                for info in bundle.infolist():
                    if info.filename == "manifest.json":
                        continue
                    relative = AgentHomeDurabilityCatalog.validate_member_name(info.filename)
                    if info.is_dir():
                        continue
                    unix_type = (info.external_attr >> 16) & 0o170000
                    if unix_type not in {0, 0o100000}:
                        raise UnsafeArchiveEntryError(f"Restore拒绝特殊ZIP条目：{info.filename}")
                    record = expected.get(relative.as_posix())
                    if record is None:
                        raise ValueError(f"归档条目未登记在Manifest：{info.filename}")
                    target = (destination / Path(*relative.parts)).resolve()
                    if destination not in target.parents:
                        raise UnsafeArchiveEntryError(f"Restore路径越界：{info.filename}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    size = 0
                    with bundle.open(info) as incoming, target.open("xb") as outgoing:
                        while chunk := incoming.read(1024 * 1024):
                            size += len(chunk)
                            if size > record.size:
                                raise ValueError(f"Restore条目大小超过Manifest：{info.filename}")
                            digest.update(chunk)
                            outgoing.write(chunk)
                        outgoing.flush()
                        os.fsync(outgoing.fileno())
                    if size != record.size or digest.hexdigest() != record.sha256:
                        raise ValueError(f"Restore条目哈希或大小不匹配：{info.filename}")
                    seen.add(relative.as_posix())
                missing = set(expected) - seen
                if missing:
                    raise ValueError(f"Restore归档缺少Manifest文件：{sorted(missing)[:5]}")
                return manifest
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        finally:
            zip_path.unlink(missing_ok=True)


def build_sources(
    home: Path,
    catalog: AgentHomeDurabilityCatalog,
) -> tuple[tuple[ArchiveSource, ...], int]:
    sources: list[ArchiveSource] = []
    logical_size = 0
    for source, relative, durability in catalog.iter_files(home):
        size = source.stat().st_size
        logical_size += size
        record = BackupFileRecord(
            path=relative.as_posix(),
            size=size,
            sha256=sha256_file(source),
            durability=durability,
        )
        sources.append(ArchiveSource(source, relative.as_posix(), record))
    return tuple(sources), logical_size


__all__ = [
    "ArchiveHeader",
    "ArchiveSource",
    "EncryptedBackupArchive",
    "build_sources",
    "sha256_file",
]
