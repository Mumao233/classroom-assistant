"""Store one API key using Windows DPAPI for the current Windows user.

Only encrypted bytes reach disk. Other platforms can use a caller-owned
in-memory key, but cannot persist keys through this module.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import tempfile
import threading
from pathlib import Path


_FORMAT = "classroom-assistant-key"
_PROTECTION = "windows-dpapi-current-user"
_MAX_KEY_BYTES = 8192
_MAX_FILE_BYTES = 65536
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class KeyStoreError(RuntimeError):
    """A user-facing failure that never includes secrets or filesystem paths."""

    def __init__(self, message: str, kind: str = "other"):
        super().__init__(message)
        self.kind = kind


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _validate_secret(secret: str) -> None:
    # Provider-specific validity is checked by the API client. A compatible
    # provider need not use a particular prefix or token length.
    if (not isinstance(secret, str) or not secret or len(secret) > _MAX_KEY_BYTES
            or any(ord(char) < 33 or ord(char) > 126 for char in secret)):
        raise KeyStoreError("API Key 不能为空，且不能包含空格、换行或非 ASCII 字符。", "invalid_key")


def _require_windows() -> None:
    if os.name != "nt":
        raise KeyStoreError("当前系统不支持加密保存 API Key，请选择仅本次使用。", "unsupported")


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    _require_windows()
    input_buffer = None
    output = _DataBlob()
    kernel32 = None
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
        description_type = ctypes.POINTER(ctypes.c_wchar_p) if decrypt else ctypes.c_wchar_p
        function.argtypes = [
            ctypes.POINTER(_DataBlob), description_type, ctypes.POINTER(_DataBlob),
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_DataBlob),
        ]
        function.restype = ctypes.c_int
        input_buffer = ctypes.create_string_buffer(data, len(data))
        source = _DataBlob(len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)))
        # Omitting CRYPTPROTECT_LOCAL_MACHINE binds encryption to this user.
        # UI_FORBIDDEN prevents a credential prompt from blocking the server.
        if not function(ctypes.byref(source), None, None, None, None,
                        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output)):
            raise OSError("DPAPI operation failed")
        if not output.pbData or not output.cbData or output.cbData > _MAX_FILE_BYTES:
            raise ValueError("Invalid DPAPI output")
        return ctypes.string_at(output.pbData, output.cbData)
    except Exception:
        if decrypt:
            raise KeyStoreError("无法解密已保存的 API Key，请在当前 Windows 账号下重新填写。", "decrypt_failed") from None
        raise KeyStoreError("无法加密 API Key，密钥未保存。", "encrypt_failed") from None
    finally:
        if input_buffer is not None:
            ctypes.memset(input_buffer, 0, ctypes.sizeof(input_buffer))
        if output.pbData and kernel32 is not None:
            ctypes.memset(output.pbData, 0, output.cbData)
            kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))


class KeyStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def save(self, secret: str) -> None:
        """Encrypt and atomically replace the saved key; reject invalid input."""
        _validate_secret(secret)
        _require_windows()
        with self._lock:
            temporary = None
            try:
                encrypted = _dpapi(secret.encode("ascii"), decrypt=False)
                payload = json.dumps({
                    "format": _FORMAT,
                    "version": 1,
                    "protection": _PROTECTION,
                    "ciphertext": base64.b64encode(encrypted).decode("ascii"),
                }, separators=(",", ":")).encode("ascii")
                if not encrypted or len(payload) > _MAX_FILE_BYTES:
                    raise KeyStoreError("无法加密 API Key，密钥未保存。", "encrypt_failed")
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, name = tempfile.mkstemp(prefix=".api-key-", suffix=".tmp", dir=self.path.parent)
                temporary = Path(name)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            except KeyStoreError:
                raise
            except Exception:
                raise KeyStoreError("无法保存 API Key，请检查本机存储是否可用。", "write_failed") from None
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass

    def load(self) -> str | None:
        """Read a supported encrypted key, or return None when none is saved."""
        with self._lock:
            try:
                with self.path.open("rb") as stream:
                    payload = stream.read(_MAX_FILE_BYTES + 1)
            except FileNotFoundError:
                return None
            except Exception:
                raise KeyStoreError("无法读取已保存的 API Key，请重新填写。", "read_failed") from None
            _require_windows()
            try:
                if len(payload) > _MAX_FILE_BYTES:
                    raise ValueError("Key file is too large")
                envelope = json.loads(payload.decode("ascii"))
                if (not isinstance(envelope, dict)
                        or set(envelope) != {"format", "version", "protection", "ciphertext"}
                        or envelope["format"] != _FORMAT
                        or type(envelope["version"]) is not int or envelope["version"] != 1
                        or envelope["protection"] != _PROTECTION
                        or not isinstance(envelope["ciphertext"], str)):
                    raise ValueError("Unrecognized encrypted key format")
                encrypted = base64.b64decode(envelope["ciphertext"], validate=True)
                if not encrypted:
                    raise ValueError("Empty encrypted key")
            except Exception:
                raise KeyStoreError("已保存的 API Key 格式无效，请重新填写并保存。", "invalid_format") from None
            try:
                secret = _dpapi(encrypted, decrypt=True).decode("ascii")
                _validate_secret(secret)
                return secret
            except KeyStoreError as error:
                if error.kind == "invalid_key":
                    raise KeyStoreError("已保存的 API Key 无效，请重新填写。", "decrypt_failed") from None
                raise
            except Exception:
                raise KeyStoreError("无法解密已保存的 API Key，请重新填写。", "decrypt_failed") from None

    def delete(self) -> None:
        """Remove the encrypted file; deleting an absent key is a no-op."""
        with self._lock:
            try:
                self.path.unlink(missing_ok=True)
            except Exception:
                raise KeyStoreError("无法删除已保存的 API Key，请检查本机存储是否可用。", "delete_failed") from None
