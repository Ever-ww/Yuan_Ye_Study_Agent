"""Encrypted, compressed, content-addressed storage for snapshot members."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .archive import _crypto, _fsync_directory, _replace_with_retry
from .models import BackupManifest
from .security import BackupSecret


OBJECT_MAGIC = b"YYOBJECT\x01"
OBJECT_TAG_BYTES = 16
OBJECT_NONCE_BYTES = 12
OBJECT_HEADER_LIMIT = 16 * 1024
STORE_SALT_BYTES = 16
STORE_SCRYPT_N = 1 << 15


class ObjectHeader(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format_version: int = 1
    algorithm: str = "AES-256-GCM"
    compression: str = "zlib"
    nonce: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_size: int = Field(ge=0)


class ObjectStoreMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format_version: int = 1
    salt: str
    key_mode: Literal["passphrase", "os_managed"]
    key_id: str | None = None
    key_verifier: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ObjectStoreContext:
    key: bytes
    metadata: ObjectStoreMetadata


class BackupObjectStore:
    """One immutable object per plaintext SHA-256 within an Agent backup root."""

    def __init__(self, backup_root: Path) -> None:
        self.backup_root = backup_root.resolve()
        self.objects_root = self.backup_root / "objects"
        self.metadata_path = self.backup_root / "control" / "backup" / "object_store.json"

    def prepare(self, secret: BackupSecret) -> ObjectStoreContext:
        if self.metadata_path.is_file():
            metadata = ObjectStoreMetadata.model_validate_json(
                self.metadata_path.read_text(encoding="utf-8"), strict=True,
            )
            if metadata.key_mode != secret.mode or metadata.key_id != secret.key_id:
                raise ValueError(
                    "Backup Object Store 已绑定另一种密钥；请沿用原密钥或使用独立 backup_directory",
                )
            context = self._derive(secret, metadata)
            self._validate_verifier(context)
            return context

        salt = secrets.token_bytes(STORE_SALT_BYTES)
        provisional = ObjectStoreMetadata(
            salt=base64.b64encode(salt).decode("ascii"),
            key_mode=secret.mode,
            key_id=secret.key_id,
            key_verifier="0" * 64,
        )
        key = self._derive_key(secret.value, salt)
        metadata = provisional.model_copy(update={"key_verifier": self._verifier(key)})
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.metadata_path.with_name(f".{self.metadata_path.name}.{uuid4().hex}.partial")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(metadata.model_dump_json(indent=2))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                # Do not replace a store identity concurrently created by a
                # second process.  The operation DB normally prevents this;
                # this check keeps the storage primitive safe on its own.
                os.link(temporary, self.metadata_path)
                temporary.unlink(missing_ok=True)
                _fsync_directory(self.metadata_path.parent)
            except FileExistsError:
                temporary.unlink(missing_ok=True)
                return self.prepare(secret)
            except OSError:
                if self.metadata_path.exists():
                    temporary.unlink(missing_ok=True)
                    return self.prepare(secret)
                _replace_with_retry(temporary, self.metadata_path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return ObjectStoreContext(key=key, metadata=metadata)

    def context_from_manifest(
        self,
        manifest: BackupManifest,
        secret: BackupSecret,
    ) -> ObjectStoreContext:
        if not manifest.object_store_salt or not manifest.key_verifier:
            raise ValueError("Snapshot Manifest 缺少 Object Store 密钥元数据")
        metadata = ObjectStoreMetadata(
            salt=manifest.object_store_salt,
            key_mode=manifest.encryption_mode or secret.mode,
            key_id=manifest.key_id,
            key_verifier=manifest.key_verifier,
        )
        if metadata.key_mode != secret.mode or metadata.key_id != secret.key_id:
            raise ValueError("Snapshot Manifest 与当前恢复密钥不匹配")
        context = self._derive(secret, metadata)
        self._validate_verifier(context)
        return context

    def object_path(self, object_id: str) -> Path:
        if len(object_id) != 64 or any(value not in "0123456789abcdef" for value in object_id):
            raise ValueError("Invalid backup object id")
        return self.objects_root / object_id[:2] / object_id

    def cleanup_interrupted_partials(self) -> tuple[Path, ...]:
        removed: list[Path] = []
        if self.objects_root.is_dir():
            for path in self.objects_root.glob("*/.*.partial"):
                if path.is_file():
                    path.unlink()
                    removed.append(path)
        return tuple(removed)

    def put(
        self,
        source: Path,
        *,
        content_hash: str,
        context: ObjectStoreContext,
    ) -> tuple[str, int, bool]:
        object_id = content_hash
        target = self.object_path(object_id)
        if target.is_file():
            header = self.read_header(target)
            if header.content_sha256 != content_hash or header.original_size != source.stat().st_size:
                raise RuntimeError(f"Backup Object identity conflict: {object_id}")
            return object_id, target.stat().st_size, False

        target.parent.mkdir(parents=True, exist_ok=True)
        nonce = secrets.token_bytes(OBJECT_NONCE_BYTES)
        header = ObjectHeader(
            nonce=base64.b64encode(nonce).decode("ascii"),
            content_sha256=content_hash,
            original_size=source.stat().st_size,
        )
        header_bytes = json.dumps(
            header.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        aad = OBJECT_MAGIC + len(header_bytes).to_bytes(4, "big") + header_bytes
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.partial")
        Cipher, algorithms, modes, _ = _crypto()
        encryptor = Cipher(algorithms.AES(context.key), modes.GCM(nonce)).encryptor()
        encryptor.authenticate_additional_data(aad)
        compressor = zlib.compressobj(level=6)
        digest = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as incoming, temporary.open("xb") as outgoing:
                outgoing.write(aad)
                while chunk := incoming.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                    compressed = compressor.compress(chunk)
                    if compressed:
                        outgoing.write(encryptor.update(compressed))
                tail = compressor.flush()
                if tail:
                    outgoing.write(encryptor.update(tail))
                outgoing.write(encryptor.finalize())
                outgoing.write(encryptor.tag)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if size != header.original_size or digest.hexdigest() != content_hash:
                raise RuntimeError(f"Staged backup member changed while storing: {source}")
            try:
                os.link(temporary, target)
                temporary.unlink(missing_ok=True)
                _fsync_directory(target.parent)
                created = True
            except FileExistsError:
                temporary.unlink(missing_ok=True)
                existing = self.read_header(target)
                if existing.content_sha256 != content_hash or existing.original_size != size:
                    raise RuntimeError(f"Concurrent backup object conflict: {object_id}")
                created = False
            except OSError:
                if target.exists():
                    temporary.unlink(missing_ok=True)
                    existing = self.read_header(target)
                    if existing.content_sha256 != content_hash or existing.original_size != size:
                        raise RuntimeError(f"Concurrent backup object conflict: {object_id}")
                    created = False
                else:
                    _replace_with_retry(temporary, target)
                    created = True
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return object_id, target.stat().st_size, created

    def restore_object(
        self,
        object_id: str,
        destination: Path,
        context: ObjectStoreContext,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.partial")
        try:
            with temporary.open("xb") as output:
                self._decrypt(self.object_path(object_id), context, output)
                output.flush()
                os.fsync(output.fileno())
            _replace_with_retry(temporary, destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def verify_object(self, object_id: str, context: ObjectStoreContext) -> ObjectHeader:
        return self._decrypt(self.object_path(object_id), context, None)

    def read_header(self, path: Path) -> ObjectHeader:
        with path.open("rb") as source:
            magic = source.read(len(OBJECT_MAGIC))
            length_bytes = source.read(4)
            if magic != OBJECT_MAGIC or len(length_bytes) != 4:
                raise ValueError(f"Invalid backup object: {path}")
            length = int.from_bytes(length_bytes, "big")
            if not 1 <= length <= OBJECT_HEADER_LIMIT:
                raise ValueError("Backup object header length is invalid")
            header = ObjectHeader.model_validate_json(source.read(length), strict=True)
        if header.format_version != 1 or header.algorithm != "AES-256-GCM" or header.compression != "zlib":
            raise ValueError("Unsupported backup object format")
        nonce = base64.b64decode(header.nonce, validate=True)
        if len(nonce) != OBJECT_NONCE_BYTES:
            raise ValueError("Backup object nonce is invalid")
        return header

    def _decrypt(
        self,
        path: Path,
        context: ObjectStoreContext,
        output: BinaryIO | None,
    ) -> ObjectHeader:
        total = path.stat().st_size
        with path.open("rb") as source:
            magic = source.read(len(OBJECT_MAGIC))
            length_bytes = source.read(4)
            if magic != OBJECT_MAGIC or len(length_bytes) != 4:
                raise ValueError(f"Invalid backup object: {path}")
            length = int.from_bytes(length_bytes, "big")
            if not 1 <= length <= OBJECT_HEADER_LIMIT:
                raise ValueError("Backup object header length is invalid")
            header_bytes = source.read(length)
            header = ObjectHeader.model_validate_json(header_bytes, strict=True)
            if (
                header.format_version != 1
                or header.algorithm != "AES-256-GCM"
                or header.compression != "zlib"
            ):
                raise ValueError("Unsupported backup object format")
            aad = magic + length_bytes + header_bytes
            ciphertext_length = total - len(aad) - OBJECT_TAG_BYTES
            if ciphertext_length < 0:
                raise ValueError("Backup object is truncated")
            source.seek(total - OBJECT_TAG_BYTES)
            tag = source.read(OBJECT_TAG_BYTES)
            source.seek(len(aad))
            nonce = base64.b64decode(header.nonce, validate=True)
            if len(nonce) != OBJECT_NONCE_BYTES:
                raise ValueError("Backup object nonce is invalid")
            Cipher, algorithms, modes, _ = _crypto()
            decryptor = Cipher(algorithms.AES(context.key), modes.GCM(nonce, tag)).decryptor()
            decryptor.authenticate_additional_data(aad)
            decompressor = zlib.decompressobj()
            digest = hashlib.sha256()
            size = 0
            remaining = ciphertext_length
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("Backup object ciphertext is truncated")
                remaining -= len(chunk)
                plain = decompressor.decompress(decryptor.update(chunk))
                if plain:
                    digest.update(plain)
                    size += len(plain)
                    if output is not None:
                        output.write(plain)
            final_compressed = decryptor.finalize()
            tail = decompressor.decompress(final_compressed) + decompressor.flush()
            if tail:
                digest.update(tail)
                size += len(tail)
                if output is not None:
                    output.write(tail)
        if size != header.original_size or digest.hexdigest() != header.content_sha256:
            raise ValueError(f"Backup object hash mismatch: {path.name}")
        return header

    @staticmethod
    def _derive_key(value: str, salt: bytes) -> bytes:
        if not value:
            raise ValueError("Backup secret cannot be empty")
        _, _, _, Scrypt = _crypto()
        return Scrypt(salt=salt, length=32, n=STORE_SCRYPT_N, r=8, p=1).derive(
            value.encode("utf-8"),
        )

    def _derive(self, secret: BackupSecret, metadata: ObjectStoreMetadata) -> ObjectStoreContext:
        salt = base64.b64decode(metadata.salt, validate=True)
        if len(salt) != STORE_SALT_BYTES:
            raise ValueError("Backup Object Store salt is invalid")
        return ObjectStoreContext(self._derive_key(secret.value, salt), metadata)

    @staticmethod
    def _verifier(key: bytes) -> str:
        return hmac.new(key, b"yy-agent-backup-object-store-v1", hashlib.sha256).hexdigest()

    def _validate_verifier(self, context: ObjectStoreContext) -> None:
        if not hmac.compare_digest(self._verifier(context.key), context.metadata.key_verifier):
            raise ValueError("Backup secret does not unlock this Object Store")


__all__ = [
    "BackupObjectStore",
    "ObjectHeader",
    "ObjectStoreContext",
    "ObjectStoreMetadata",
]
