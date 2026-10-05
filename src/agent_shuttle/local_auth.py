"""Per-process credentials for the loopback A2A transport."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class PeerAuthenticationError(RuntimeError):
    """The local peer could not prove possession of its published credential."""


def local_origin(url: str) -> str | None:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        return None
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or port is None
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        return None
    return f"http://127.0.0.1:{port}"


def _directory() -> Path:
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "AgentShuttle" / "run"
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base) / "agent-shuttle"
    return Path.home() / ".local" / "state" / "agent-shuttle" / "run"


def _windows_acl(path: Path, *, set_acl: bool = False) -> None:
    import win32api
    import win32security
    import ntsecuritycon

    process_token = win32security.OpenProcessToken(win32api.GetCurrentProcess(),
                                                  win32security.TOKEN_QUERY)
    sid = win32security.GetTokenInformation(process_token, win32security.TokenUser)[0]
    acl = win32security.ACL()
    acl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid)
    if set_acl:
        descriptor = win32security.SECURITY_DESCRIPTOR()
        descriptor.SetSecurityDescriptorOwner(sid, False)
        descriptor.SetSecurityDescriptorDacl(1, acl, 0)
        win32security.SetFileSecurity(str(path),
            win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
            descriptor)
    actual = win32security.GetFileSecurity(str(path),
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION)
    dacl = actual.GetSecurityDescriptorDacl()
    if actual.GetSecurityDescriptorOwner() != sid or dacl is None or dacl.GetAceCount() != 1:
        raise PermissionError(f"Unsafe Agent Shuttle credential ACL: {path}")
    ace = dacl.GetAce(0)
    if ace[2] != sid or ace[1] != ntsecuritycon.FILE_ALL_ACCESS:
        raise PermissionError(f"Unsafe Agent Shuttle credential ACL: {path}")


def _check(path: Path, directory: bool) -> None:
    if path.is_symlink():
        raise PermissionError(f"Symlink in Agent Shuttle credential path: {path}")
    info = path.stat()
    if directory != stat.S_ISDIR(info.st_mode):
        raise PermissionError(f"Invalid Agent Shuttle credential path: {path}")
    if os.name == "nt":
        _windows_acl(path)
    elif info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError(f"Unsafe Agent Shuttle credential permissions: {path}")


def _prepare_directory() -> Path:
    path = _directory()
    created = not path.exists()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "nt" and created:
        _windows_acl(path, set_acl=True)
    _check(path, True)
    return path


def _record_path(port: int) -> Path:
    return _directory() / f"{port}.json"


@dataclass(frozen=True)
class LocalCredential:
    origin: str
    token: str
    instance_id: str

    @classmethod
    def fresh(cls, origin: str) -> "LocalCredential":
        canonical = local_origin(origin)
        if canonical != origin.rstrip("/"):
            raise ValueError("A2A server must use an exact 127.0.0.1 HTTP URL with a port")
        return cls(canonical, secrets.token_urlsafe(32), str(uuid.uuid4()))

    @property
    def port(self) -> int:
        return int(self.origin.rsplit(":", 1)[1])

    def signature(self, nonce: str) -> str:
        payload = f"agent-shuttle-proof-v1\n{nonce}\n{self.instance_id}\n{self.origin}".encode()
        return hmac.new(self.token.encode(), payload, hashlib.sha256).hexdigest()

    def publish(self) -> None:
        directory = _prepare_directory()
        path = directory / f"{self.port}.json"
        fd, temporary = tempfile.mkstemp(prefix=f".{self.port}-", dir=directory)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            else:
                _windows_acl(Path(temporary), set_acl=True)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump({"origin": self.origin, "token": self.token,
                           "instance_id": self.instance_id, "pid": os.getpid()}, output)
                output.flush()
                os.fsync(output.fileno())
            _check(Path(temporary), False)
            os.replace(temporary, path)
            _check(path, False)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def remove_if_owned(self) -> None:
        path = _record_path(self.port)
        try:
            record = _read(self.origin)
            if record and record.instance_id == self.instance_id:
                path.unlink()
        except FileNotFoundError:
            pass


def _read(origin: str) -> LocalCredential | None:
    canonical = local_origin(origin)
    if canonical is None:
        return None
    directory = _directory()
    if not directory.exists():
        return None
    _check(directory, True)
    path = _record_path(int(canonical.rsplit(":", 1)[1]))
    if not path.exists():
        return None
    _check(path, False)
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("origin") != canonical:
        raise PeerAuthenticationError("Local credential address does not match the peer")
    return LocalCredential(data["origin"], data["token"], data["instance_id"])


def read_local_credential(origin: str) -> LocalCredential | None:
    try:
        record = _read(origin)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PeerAuthenticationError(f"Unsafe or invalid local credential for {origin}") from exc
    if record is not None and (not isinstance(record.token, str)
                               or not isinstance(record.instance_id, str)
                               or len(record.token) < 40):
        raise PeerAuthenticationError(f"Invalid local credential for {origin}")
    return record


def new_nonce() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
