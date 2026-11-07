"""S3 / Cloudflare R2 backend. boto3 is imported lazily, inside methods only."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any, NoReturn

from ..errors import BackendError, BlobExists, ConfigError, HeadConflict
from ..model import (
    SECRET_S3_ACCESS_KEY_ID,
    SECRET_S3_SECRET_ACCESS_KEY,
)
from ..textsafe import untrusted
from . import validate_key

_COPY_CHUNK = 1024 * 1024
_MAX_HEAD_BYTES = 1024 * 1024
_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}


def _error_shape(exc: Exception) -> tuple[str, int | None]:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        response = {}
    error = response.get("Error")
    metadata = response.get("ResponseMetadata")
    code = ""
    if isinstance(error, dict):
        code = str(error.get("Code", ""))
    status = None
    if isinstance(metadata, dict):
        status = metadata.get("HTTPStatusCode")
    return code, status


def _is_not_found(exc: Exception) -> bool:
    code, status = _error_shape(exc)
    return code in _NOT_FOUND_CODES or status == 404


class S3Backend:
    """S3/R2 object store.

    HEAD lives at ``{prefix}/HEAD``; its version token is the server ETag.
    Compare-and-swap uses ``IfMatch``/``IfNoneMatch`` on ``put_object`` and
    the server rejects stale writes with HTTP 412.
    """

    name = "s3"

    def __init__(
        self,
        bucket: str,
        prefix: str,
        *,
        endpoint_url: str = "",
        region: str = "",
        access_key_id: str = "",
        secret_access_key: str = "",
    ) -> None:
        if not bucket:
            raise ConfigError("s3 backend needs s3_bucket: set it in the roam config")
        if not access_key_id:
            raise ConfigError(f"s3 backend needs the {SECRET_S3_ACCESS_KEY_ID} secret")
        if not secret_access_key:
            raise ConfigError(f"s3 backend needs the {SECRET_S3_SECRET_ACCESS_KEY} secret")
        self.bucket = bucket
        self.prefix = prefix.strip("/") if prefix else ""
        self.endpoint_url = endpoint_url or None
        self.region = region or None
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self._client_cache: Any = None

    def __repr__(self) -> str:
        return (
            f"S3Backend(bucket={self.bucket!r}, prefix={self.prefix!r}, "
            f"endpoint_url={self.endpoint_url!r})"
        )

    @property
    def _client(self) -> Any:
        if self._client_cache is None:
            if self.endpoint_url and not self.endpoint_url.startswith(("http://", "https://")):
                raise ConfigError(
                    f"s3 endpoint_url must start with http:// or https://, "
                    f"got {self.endpoint_url!r}: set a full endpoint URL in the roam config"
                )
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:  # pragma: no cover - depends on install
                raise BackendError(
                    "boto3 is not installed; install it with: pip install 'boto3>=1.34,<2'"
                ) from exc
            config_kwargs: dict[str, Any] = {
                # Fail fast: a blackholed endpoint must not stall the engine.
                "connect_timeout": 5,
                "read_timeout": 30,
                "retries": {"max_attempts": 2, "mode": "standard"},
            }
            if self.endpoint_url:
                config_kwargs["s3"] = {"addressing_style": "path"}
            try:
                self._client_cache = boto3.client(
                    "s3",
                    endpoint_url=self.endpoint_url,
                    region_name=self.region,
                    aws_access_key_id=self.access_key_id,
                    aws_secret_access_key=self.secret_access_key,
                    config=Config(**config_kwargs),
                )
            except Exception as exc:
                raise ConfigError(
                    f"s3 endpoint_url {self.endpoint_url!r} is not usable: {exc}"
                ) from exc
        return self._client_cache

    def _head_key(self) -> str:
        return f"{self.prefix}/HEAD" if self.prefix else "HEAD"

    def _key(self, key: str) -> str:
        canonical = validate_key(key)
        return f"{self.prefix}/{canonical}" if self.prefix else canonical

    @staticmethod
    def _etag(value: object) -> str:
        return str(value).strip('"')

    def _raise(self, exc: Exception, *, key: str, conflict: type[BackendError]) -> NoReturn:
        """Translate a botocore failure into a roam error. Never returns.

        Server-controlled error text (the Code a malicious S3-compatible
        endpoint chooses) enters the message only through
        textsafe.untrusted(); failures without a server error code are
        local (network/credential) and are named by exception class only.
        """
        code, status = _error_shape(exc)
        if code in {"PreconditionFailed", "412"} or status == 412:
            raise conflict(
                f"the remote refused the conditional write for {key}: it changed since it was "
                "read; pull first"
            ) from exc
        if code:
            raise BackendError(f"s3 request for {key} failed: {untrusted(code)}") from exc
        raise BackendError(
            f"s3 request for {key} failed: {type(exc).__name__}; check the endpoint, "
            "region, and credentials"
        ) from exc

    @staticmethod
    def _check_deadline(deadline: float | None, *, what: str) -> None:
        """R8: one budget per user operation. Each individual request is
        bounded by the client's connect/read timeouts; this check makes the
        shared operation budget fail closed before another request starts."""
        if deadline is not None and deadline - time.monotonic() <= 0:
            raise BackendError(
                f"s3 {what} exceeded its operation deadline; "
                "the remote is unreachable or too slow"
            )

    def get_head(self, *, deadline: float | None = None) -> tuple[bytes, str] | None:
        self._check_deadline(deadline, what="get_head")
        key = self._head_key()
        client = self._client
        try:
            head = client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if _is_not_found(exc):
                return None
            self._raise(exc, key=key, conflict=HeadConflict)
        size = int(head["ContentLength"])
        if size > _MAX_HEAD_BYTES:
            raise BackendError(f"head {key} is {size} bytes, over the {_MAX_HEAD_BYTES} byte limit")
        body: Any = None
        try:
            result = client.get_object(Bucket=self.bucket, Key=key)
            body = result["Body"]
            data = body.read(_MAX_HEAD_BYTES + 1)
        except Exception as exc:
            self._raise(exc, key=key, conflict=HeadConflict)
        finally:
            if body is not None:
                body.close()
        if len(data) > _MAX_HEAD_BYTES or len(data) != size:
            raise BackendError(f"head {key} changed while it was downloaded")
        return bytes(data), self._etag(head["ETag"])

    def put_head(
        self, data: bytes, expected: str | None, *, deadline: float | None = None
    ) -> str:
        self._check_deadline(deadline, what="put_head")
        key = self._head_key()
        conditions = {"IfMatch": expected} if expected is not None else {"IfNoneMatch": "*"}
        try:
            result = self._client.put_object(Bucket=self.bucket, Key=key, Body=data, **conditions)
        except Exception as exc:
            self._raise(exc, key=key, conflict=HeadConflict)
        token = self._etag(result.get("ETag", ""))
        if not token:
            try:
                token = self._etag(self._client.head_object(Bucket=self.bucket, Key=key)["ETag"])
            except Exception as exc:
                self._raise(exc, key=key, conflict=HeadConflict)
        return token

    def put_blob(
        self, key: str, source: Path, *, deadline: float | None = None
    ) -> None:
        """Store one immutable bundle as a SINGLE conditional PUT.

        ``put_object`` with an open file body never goes multipart — boto3
        only switches to multipart for ``upload_file``/``upload_fileobj``,
        which are never used here — so the ``IfNoneMatch="*"`` create-once
        condition is evaluated atomically by the server (R2 documents this
        exact semantic; AWS S3 added conditional PutObject the same way) and
        a concurrent writer can never also observe success. Bundles are
        capped by ``max_bundle_bytes`` at the engine layer and must stay
        under the single-PUT ceiling (5 GiB on S3 and R2).
        """
        self._check_deadline(deadline, what="put_blob")
        full_key = self._key(key)
        src = Path(source)
        if not src.is_file() or src.is_symlink():
            raise BackendError(f"bundle source must be a regular file: {src}")
        try:
            with src.open("rb") as body:
                self._client.put_object(
                    Bucket=self.bucket, Key=full_key, Body=body, IfNoneMatch="*"
                )
        except Exception as exc:
            self._raise(exc, key=key, conflict=BlobExists)

    def get_blob(
        self,
        key: str,
        dest: Path,
        *,
        max_bytes: int,
        deadline: float | None = None,
    ) -> None:
        self._check_deadline(deadline, what="get_blob")
        full_key = self._key(key)
        client = self._client
        try:
            head = client.head_object(Bucket=self.bucket, Key=full_key)
        except Exception as exc:
            self._raise(exc, key=key, conflict=BlobExists)
        size = int(head["ContentLength"])
        if size > max_bytes:
            raise BackendError(f"bundle {key} is {size} bytes, over the {max_bytes} byte limit")
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        body: Any = None
        try:
            self._check_deadline(deadline, what="get_blob")  # before the stream
            result = client.get_object(Bucket=self.bucket, Key=full_key)
            body = result["Body"]
            with temp.open("xb") as writer:
                written = 0
                while chunk := body.read(_COPY_CHUNK):
                    if deadline is not None:
                        self._check_deadline(deadline, what="get_blob")
                    written += len(chunk)
                    if written > max_bytes:
                        raise BackendError(f"bundle {key} exceeded the {max_bytes} byte limit")
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            if written != size:
                raise BackendError(f"bundle {key} changed while it was downloaded")
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
            if body is not None:
                body.close()

    def blob_exists(self, key: str, *, deadline: float | None = None) -> bool:
        self._check_deadline(deadline, what="blob_exists")
        full_key = self._key(key)
        try:
            self._client.head_object(Bucket=self.bucket, Key=full_key)
            return True
        except Exception as exc:
            if _is_not_found(exc):
                return False
            self._raise(exc, key=key, conflict=BlobExists)

    def list_blobs(self, prefix: str, *, deadline: float | None = None) -> list[str]:
        """Sorted store keys under ``prefix`` (0.4 history/restore). Bounded:
        at most 100 pages; each request is capped by the client timeouts."""
        self._check_deadline(deadline, what="list_blobs")
        if not isinstance(prefix, str) or not prefix:
            raise ConfigError(f"invalid blob prefix {prefix!r}: prefixes must be non-empty strings")
        try:
            validate_key(prefix.rstrip("/"))
        except ConfigError:
            raise ConfigError(
                f"invalid blob prefix {prefix!r}: use a relative POSIX prefix such as 'bundles/'"
            ) from None
        full_prefix = f"{self.prefix}/{prefix}" if self.prefix else prefix
        strip = len(f"{self.prefix}/") if self.prefix else 0
        keys: list[str] = []
        token: str | None = None
        for _ in range(100):
            self._check_deadline(deadline, what="list_blobs")
            request: dict[str, Any] = {"Bucket": self.bucket, "Prefix": full_prefix}
            if token:
                request["ContinuationToken"] = token
            try:
                page = self._client.list_objects_v2(**request)
            except Exception as exc:
                code, _ = _error_shape(exc)
                if code:
                    raise BackendError(f"s3 list for {prefix} failed: {untrusted(code)}") from exc
                raise BackendError(
                    f"s3 list for {prefix} failed: {type(exc).__name__}; check the "
                    "endpoint, region, and credentials"
                ) from exc
            for item in page.get("Contents", []) or []:
                key = str(item.get("Key", ""))[strip:]
                try:
                    validate_key(key)
                except ConfigError:
                    continue
                if key.startswith(prefix):
                    keys.append(key)
            if not page.get("IsTruncated"):
                return sorted(set(keys))
            token = page.get("NextContinuationToken")
        return sorted(set(keys))

    def ping(self, *, deadline: float | None = None) -> str:
        self._check_deadline(deadline, what="ping")
        client = self._client
        try:
            client.head_bucket(Bucket=self.bucket)
        except Exception as head_exc:
            if _is_not_found(head_exc):
                raise BackendError(
                    f"s3 unreachable: bucket {self.bucket!r} does not exist"
                ) from head_exc
            try:
                client.list_objects_v2(Bucket=self.bucket, MaxKeys=1, Prefix=self.prefix)
            except Exception as list_exc:
                code, _ = _error_shape(list_exc)
                if code:
                    raise BackendError(f"s3 unreachable: {untrusted(code)}") from list_exc
                raise BackendError(
                    f"s3 unreachable: {type(list_exc).__name__}; check the endpoint, "
                    "region, and credentials"
                ) from list_exc
        return f"s3 backend ready: bucket {self.bucket!r} reachable (prefix {self.prefix!r})"
