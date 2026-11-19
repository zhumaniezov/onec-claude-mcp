"""Per-user, local-computer generic Windows credentials scoped to a 1C publication."""

import getpass
import hashlib
import json
import re
import sys
import warnings
from contextlib import contextmanager

_ALIAS = re.compile(r"[A-Za-z0-9_-]{1,64}")
_BEARER = re.compile(r"[A-Za-z0-9._~+/-]+=*")
_MAX_BLOB = 5 * 512  # CRED_MAX_CREDENTIAL_BLOB_SIZE (wincred.h)
_INVALID = "Недопустимые или повреждённые учётные данные 1С."
_UNAVAILABLE = "Хранилище учётных данных Windows недоступно в текущем сеансе."
_EXISTS = "Запись уже существует. Для её замены укажите --replace."
_NO_CONSOLE = "Для скрытого ввода откройте интерактивную консоль Windows."


def validate_credential_alias(alias):
    if not isinstance(alias, str) or not _ALIAS.fullmatch(alias):
        raise ValueError("credentialRef должен содержать 1–64 символа A–Z, a–z, 0–9, '_' или '-'.")
    return alias


def credential_target(alias, publication_url):
    """Use the canonical publication root supplied by the profile parser."""
    alias = validate_credential_alias(alias)
    if not isinstance(publication_url, str) or not publication_url:
        raise ValueError("Не задан адрес публикации для учётных данных.")
    digest = hashlib.sha256(publication_url.encode("utf-8")).hexdigest()
    return f"OneC-Claude-MCP/{alias.lower()}/{digest}"


def _windows_store():
    if sys.platform != "win32":
        raise ValueError("Хранилище учётных данных Windows доступно только в Windows.")
    try:
        import pywintypes
        import win32cred
    except (ImportError, OSError):
        raise ValueError("Компонент pywin32 для хранилища Windows недоступен.") from None
    return win32cred, pywintypes.error


def _read(store, native_error, target):
    try:
        return store.CredRead(target, store.CRED_TYPE_GENERIC, 0)
    except native_error as error:
        if error.winerror == 1168:  # ERROR_NOT_FOUND
            return None
        raise ValueError(_UNAVAILABLE) from None


def _printable(value):
    return (
        isinstance(value, str)
        and bool(value)
        and all(ord(c) >= 32 and ord(c) != 127 and not 0xD800 <= ord(c) <= 0xDFFF for c in value)
    )


def _options(record):
    if not isinstance(record, dict) or type(record.get("v")) is not int or record["v"] != 1:
        raise ValueError(_INVALID)
    secret = record.get("secret")
    if not _printable(secret):
        raise ValueError(_INVALID)
    if record.get("mode") == "basic" and set(record) == {"v", "mode", "username", "secret"}:
        username = record["username"]
        if _printable(username) and ":" not in username:
            return {"username": username, "password": secret, "token": None}
    if (
        record.get("mode") == "bearer"
        and set(record) == {"v", "mode", "secret"}
        and _BEARER.fullmatch(secret)
    ):
        return {"username": None, "password": None, "token": secret}
    raise ValueError(_INVALID)


def _decode(blob):
    if not isinstance(blob, bytes) or not 0 < len(blob) <= _MAX_BLOB:
        raise ValueError(_INVALID)
    try:
        record = json.loads(blob.decode("utf-16-le"))
    except (ValueError, RecursionError):
        raise ValueError(_INVALID) from None
    return _options(record)


def read_windows_credential(alias, publication_url):
    target = credential_target(alias, publication_url)
    store, native_error = _windows_store()
    entry = _read(store, native_error, target)
    if entry is None:
        raise ValueError("Для этой публикации в хранилище Windows нет записи. Выполните --credential-set.")
    if entry.get("Persist") != store.CRED_PERSIST_LOCAL_MACHINE:
        raise ValueError(_INVALID)
    return _decode(entry.get("CredentialBlob"))


def _console_attached():
    if sys.platform != "win32" or sys.stdin is None or not sys.stdin.isatty():
        return False
    try:
        import pywintypes
        import win32console
    except (ImportError, OSError):
        return False
    try:
        win32console.GetStdHandle(win32console.STD_INPUT_HANDLE).GetConsoleMode()
    except (OSError, pywintypes.error):
        return False
    return True


def _prompt(label):
    if not _console_attached():
        raise ValueError(_NO_CONSOLE)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(label)
    except (getpass.GetPassWarning, EOFError, OSError):
        raise ValueError(_NO_CONSOLE) from None


@contextmanager
def _provision_lock(target):
    # CredWrite is an upsert. Serialize the final existence check and write across
    # connector processes, even when the same user has multiple Windows sessions.
    import win32api
    import win32event

    account = win32api.GetUserNameEx(2)  # NameSamCompatible
    key = hashlib.sha256((account + "/" + target).encode("utf-8")).hexdigest()
    handle = win32event.CreateMutex(None, False, "Global\\OneC-Claude-MCP-Provision-" + key)
    try:
        status = win32event.WaitForSingleObject(handle, 5000)
        if status not in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
            raise ValueError("Учётная запись обновляется другим процессом. Повторите команду.")
        try:
            yield
        finally:
            win32event.ReleaseMutex(handle)
    finally:
        handle.Close()


def provision_windows_credential(alias, publication_url, mode, *, replace=False):
    """Prompt directly in the console and create or explicitly replace one entry."""
    target = credential_target(alias, publication_url)
    if mode not in ("basic", "bearer"):
        raise ValueError("Укажите тип учётных данных basic или bearer.")
    store, native_error = _windows_store()
    if not replace and _read(store, native_error, target) is not None:
        raise ValueError(_EXISTS)
    record = {"v": 1, "mode": mode}
    if mode == "basic":
        record["username"] = _prompt("Логин 1С (ввод скрыт): ")
    record["secret"] = _prompt("Пароль 1С: " if mode == "basic" else "Bearer-токен: ")
    _options(record)
    # pywin32 CredWrite takes a Unicode string. CredRead returns its native bytes.
    blob = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    if len(blob.encode("utf-16-le")) > _MAX_BLOB:
        raise ValueError("Учётные данные превышают лимит записи хранилища Windows (2560 байт).")
    try:
        with _provision_lock(target):
            if not replace and _read(store, native_error, target) is not None:
                raise ValueError(_EXISTS)
            store.CredWrite(
                {
                    "Type": store.CRED_TYPE_GENERIC,
                    "TargetName": target,
                    "UserName": "OneC-Claude-MCP",
                    "Persist": store.CRED_PERSIST_LOCAL_MACHINE,
                    "CredentialBlob": blob,
                },
                0,
            )
    except (native_error, OSError, ImportError):
        raise ValueError(_UNAVAILABLE) from None
