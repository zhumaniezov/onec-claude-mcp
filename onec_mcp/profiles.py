"""Validate named 1C publications and resolve credentials when opening a session."""

import ipaddress
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit

from .credentials import read_windows_credential, validate_credential_alias

_PROFILE_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DEFAULTS = {
    "allowLoopbackHttp": False,
    "physicalDelete": False,
    "timeoutMs": 20000,
    "maxResponseMiB": 32,
}
_CREDENTIALS = {"usernameEnv", "passwordEnv", "tokenEnv"}
_KEYS = (
    _CREDENTIALS
    | _DEFAULTS.keys()
    | {"publicationUrl", "label", "writeCollections", "readCollections", "readFields", "credentialRef"}
)


def _bad_field(path: str) -> ValueError:
    return ValueError("Проверьте поля файла профилей: " + path)


def _names(values, path, *, field_paths=False):
    if not isinstance(values, list):
        raise _bad_field(path)
    if values == ["*"]:
        return ["*"]
    result = []
    for name in values:
        parts = name.split("/") if isinstance(name, str) else []
        if not parts or len(parts) > (2 if field_paths else 1) or not all(p.isidentifier() for p in parts):
            raise _bad_field(path)
        if name not in result:
            result.append(name)
    return result


def can_read_collection(profile, name):
    collections = profile.get("readCollections")
    if collections is not None and name not in collections and "*" not in collections:
        return False
    return "readFields" not in profile or bool(profile["readFields"].get(name))


def _publication_root(raw: str, allow_loopback: bool, name: str) -> str:
    try:
        url = urlsplit(raw.strip())
        host = url.hostname
        port = url.port
        if not host or "\\" in raw or "%" in host or any(ch.isspace() for ch in host):
            raise ValueError
        if ":" in host:
            host = ipaddress.IPv6Address(host).compressed
        else:
            host = host.encode("idna").decode("ascii").lower()
        if any(ch in host for ch in "/?#@[]"):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ValueError("Некорректный publicationUrl в профиле " + name) from None
    if url.username is not None or url.password is not None or "?" in raw or "#" in raw:
        raise ValueError("publicationUrl должен содержать только адрес публикации и путь.")
    local = host in {"localhost", "127.0.0.1", "::1"}
    if url.scheme != "https" and not (url.scheme == "http" and allow_loopback and local):
        raise ValueError("Нужен HTTPS. Для локального стенда разрешите allowLoopbackHttp.")
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != (443 if url.scheme == "https" else 80):
        authority += f":{port}"
    # Match browser URL resolution of literal and percent-encoded dot segments.
    segments = []
    for segment in url.path.split("/")[1:]:
        normalized = re.sub(r"%2e", ".", segment, flags=re.IGNORECASE)
        if normalized == "..":
            if segments:
                segments.pop()
        elif normalized != ".":
            segments.append(segment)
    path = quote("/".join(segments), safe="!$&'()*+,;=:@/%-._~")
    return f"{url.scheme}://{authority}/{path}".rstrip("/")


def parse_profiles(raw) -> dict[str, dict]:
    if not isinstance(raw, dict) or raw.keys() != {"bases"} or not isinstance(raw["bases"], dict):
        raise _bad_field("bases")
    profiles = {}
    for name, fields in raw["bases"].items():
        if not isinstance(name, str) or not _PROFILE_NAME.fullmatch(name):
            raise _bad_field("bases")
        path = "bases." + name
        if not isinstance(fields, dict) or fields.keys() - _KEYS:
            raise _bad_field(path)
        profile = {**_DEFAULTS, "writeCollections": [], **fields}
        publication = profile.get("publicationUrl")
        if not isinstance(publication, str) or not publication:
            raise _bad_field(path + ".publicationUrl")
        if "label" in fields:
            label = fields["label"]
            if not isinstance(label, str) or len(label.encode("utf-16-le", "surrogatepass")) > 240:
                raise _bad_field(path + ".label")
        env_fields = _CREDENTIALS.intersection(fields)
        if "credentialRef" in fields:
            if env_fields:
                raise ValueError("credentialRef несовместим с переменными доступа в профиле " + name)
            profile["credentialRef"] = validate_credential_alias(fields["credentialRef"])
        if env_fields and sys.platform == "win32":
            raise ValueError("На Windows замените поля доступа из окружения на credentialRef "
                             "и сохраните секрет командой --credential-set для профиля " + name)
        for field in env_fields:
            if not isinstance(fields[field], str) or not _ENV_NAME.fullmatch(fields[field]):
                raise _bad_field(path + "." + field)
        for field in ("allowLoopbackHttp", "physicalDelete"):
            if not isinstance(profile[field], bool):
                raise _bad_field(path + "." + field)
        for field, low, high in (
            ("timeoutMs", 1000, 120000),
            ("maxResponseMiB", 1, 128),
        ):
            value = profile[field]
            if type(value) not in (int, float) or not low <= value <= high or int(value) != value:
                raise _bad_field(path + "." + field)
            profile[field] = int(value)
        allowed = profile["writeCollections"]
        if not isinstance(allowed, list) or any(not isinstance(item, str) or not item for item in allowed):
            raise _bad_field(path + ".writeCollections")
        profile["writeCollections"] = list(allowed)
        if "readCollections" in fields:
            profile["readCollections"] = _names(fields["readCollections"], path + ".readCollections")
        if "readFields" in fields:
            rules = fields["readFields"]
            if not isinstance(rules, dict) or not all(isinstance(k, str) and k.isidentifier() for k in rules):
                raise _bad_field(path + ".readFields")
            profile["readFields"] = {
                k: _names(v, path + ".readFields." + k, field_paths=True) for k, v in rules.items()
            }
        root = _publication_root(publication, profile["allowLoopbackHttp"], name)
        if ("usernameEnv" in fields) != ("passwordEnv" in fields) or (
            "tokenEnv" in fields and "usernameEnv" in fields
        ):
            raise ValueError("Укажите пару usernameEnv/passwordEnv или tokenEnv в профиле " + name)
        if profile["physicalDelete"] and not allowed:
            raise ValueError("Для physicalDelete укажите writeCollections в профиле " + name)
        profile.update(
            name=name,
            publicationUrl=root,
            odataUrl=root + "/odata/standard.odata",
            extensionUrl=root + "/hs/onec_mcp/v1/",
        )
        profiles[name] = profile
    if not profiles:
        raise ValueError("В файле профилей нужен хотя бы один элемент bases.")
    return profiles


def load_profiles(path: str | Path | None) -> dict[str, dict]:
    if not path:
        raise ValueError("Передайте --config <путь к profiles.json> или переменную ONEC_MCP_CONFIG.")
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise ValueError("Не удалось прочитать JSON-файл профилей. Проверьте путь и синтаксис.") from None
    return parse_profiles(raw)


def _environment_credentials(profile, env):
    def secret(key):
        variable = profile.get(key)
        if variable is None:
            return None
        value = env.get(variable)
        if not value:
            raise ValueError("Задайте переменную окружения " + variable + " для процесса Claude Code.")
        return value

    return {
        "username": secret("usernameEnv"),
        "password": secret("passwordEnv"),
        "token": secret("tokenEnv"),
    }


def connection_options(profile: dict, env=None) -> dict:
    if "credentialRef" in profile:
        credentials = read_windows_credential(profile["credentialRef"], profile["publicationUrl"])
    else:
        credentials = _environment_credentials(profile, os.environ if env is None else env)
    return {
        **credentials,
        "allow_insecure_http": profile["allowLoopbackHttp"],
        "timeout_ms": profile["timeoutMs"],
        "max_bytes": profile["maxResponseMiB"] * 1024 * 1024,
    }
