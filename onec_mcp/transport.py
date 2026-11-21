"""Bounded, single-attempt HTTP requests to one configured 1C publication."""

import asyncio
import base64
import ipaddress
import json
import math
import re
import ssl
import zlib
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlencode, urlsplit

import httpx

MIB = 1024 * 1024
SAFE_INTEGER = 2**53 - 1
UNKNOWN_WRITE = " Результат записи может быть неизвестен. Проверьте состояние в 1С перед повтором."


class TransportError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class PostedDocumentError(TransportError):
    def __init__(self):
        super().__init__("Расширение 1С запретило операцию с проведённым документом (HTTP 422).", 422)


_MAX_MESSAGES = 20
_MAX_MESSAGE_LENGTH = 1000
_CHANGE_REFUSALS = {
    "write_rejected": "1С отклонила запись объекта.",
    "posting_failed": "1С не провела документ. Изменение не сохранено, документ и движения остались прежними.",
    "unposting_failed": "1С не отменила проведение документа. Изменение не сохранено.",
    "not_posted": "Документ не проведён. Отменять проведение не требуется.",
    "posting_not_allowed": "Документы этого вида не проводятся.",
    "posting_forbidden": "У интеграционного пользователя нет права на проведение этого документа.",
    "unposting_forbidden": "У интеграционного пользователя нет права на отмену проведения этого документа.",
}


def _user_messages(value):
    """Keep only short printable texts that the configuration addressed to a user."""
    if not isinstance(value, list):
        return []
    messages = []
    for item in value[:_MAX_MESSAGES]:
        if isinstance(item, str):
            text = "".join(ch if ch in "\n\t" or (ord(ch) >= 32 and ord(ch) != 127) else " " for ch in item)
            text = text.strip()[:_MAX_MESSAGE_LENGTH]
            if text:
                messages.append(text)
    return messages


class ChangeRefusedError(TransportError):
    """A recognised refusal of the extension's /change method. Nothing was committed."""

    def __init__(self, code, status, messages=()):
        self.code = code
        self.messages = list(messages)
        super().__init__(f"{_CHANGE_REFUSALS[code]} (HTTP {status}, {code}).", status)


def _read_integer(literal):
    if len(literal.lstrip("-")) > 16:
        return literal
    value = int(literal)
    return value if abs(value) <= SAFE_INTEGER else literal


def _read_fraction(literal):
    value = float(literal)
    if not math.isfinite(value) or (value.is_integer() and abs(value) > SAFE_INTEGER):
        return literal
    try:
        return value if Decimal(str(value)) == Decimal(literal) else literal
    except InvalidOperation:
        return literal


def _invalid_json_constant(value):
    raise ValueError("Недопустимая числовая константа JSON.")


def _encode_json(value):
    """Decimal is a validated JSON number on the wire, without a binary-float round trip."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Неконечное значение JSON.")
        return str(value)
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(
                json.dumps(key, ensure_ascii=False) + ":" + _encode_json(item) for key, item in value.items()
            )
            + "}"
        )
    if isinstance(value, list):
        return "[" + ",".join(_encode_json(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _limit(value, maximum, name):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"Недопустимое значение {name}.")
    return value


async def _read_bytes(response, limit, uncertainty):
    encoding = response.headers.get("content-encoding", "identity").lower().strip()
    if encoding not in ("identity", "gzip", "deflate"):
        raise TransportError("Неподдерживаемое сжатие ответа 1С." + uncertainty)
    decoded = bytearray()
    wire_size = 0
    limit_error = f"Размер ответа 1С превышает лимит {limit} байт." + uncertainty
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
    prefix = b""
    try:
        async for chunk in response.aiter_raw():
            wire_size += len(chunk)
            if wire_size > limit + 65536:
                raise TransportError(limit_error)
            if encoding == "deflate" and decoder is None:
                # Older proxies send raw DEFLATE; HTTP normally carries an RFC 1950 zlib envelope.
                chunk = prefix + chunk
                if len(chunk) < 2:
                    prefix = chunk
                    continue
                cmf, flags = chunk[:2]
                wrapped = cmf & 15 == 8 and cmf >> 4 <= 7 and (cmf * 256 + flags) % 31 == 0
                decoder = zlib.decompressobj(zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)
            if decoder is None:
                if len(decoded) + len(chunk) > limit:
                    raise TransportError(limit_error)
                decoded.extend(chunk)
                continue
            while chunk:
                if decoder.eof:
                    if encoding != "gzip":
                        raise zlib.error("trailing deflate data")
                    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                output = decoder.decompress(chunk, limit - len(decoded) + 1)
                if len(decoded) + len(output) > limit:
                    raise TransportError(limit_error)
                decoded.extend(output)
                chunk = decoder.unused_data if decoder.eof else decoder.unconsumed_tail
        if encoding != "identity" and (decoder is None or not decoder.eof):
            raise zlib.error("incomplete compressed response")
    except zlib.error:
        raise TransportError("Повреждён сжатый ответ 1С." + uncertainty) from None
    return decoded


class Transport:
    def __init__(
        self,
        base_url,
        *,
        username=None,
        password=None,
        token=None,
        timeout_ms=20000,
        max_bytes=32 * MIB,
        allow_insecure_http=False,
    ):
        address = urlsplit(base_url)
        if (
            not address.hostname
            or address.username is not None
            or address.password is not None
            or address.query
            or address.fragment
            or re.search(r"[\x00-\x20\\]", base_url)
        ):
            raise ValueError("URL должен содержать только адрес публикации и путь.")
        try:
            local = ipaddress.ip_address(address.hostname).is_loopback
        except ValueError:
            local = address.hostname == "localhost"
        if address.scheme != "https" and not (
            address.scheme == "http" and local and allow_insecure_http is True
        ):
            raise ValueError("Требуется HTTPS. Локальный HTTP нужно разрешить в профиле.")
        if (
            (token and (username or password))
            or (password and not username)
            or (username and ":" in username)
        ):
            raise ValueError("Некорректное сочетание учётных данных профиля.")
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = _limit(timeout_ms, 300000, "timeout_ms") / 1000
        self.max_bytes = _limit(max_bytes, 128 * MIB, "max_bytes")
        self.ssl_context = ssl.create_default_context()
        self.authorization = None
        if username:
            basic = base64.b64encode(f"{username}:{password or ''}".encode()).decode("ascii")
            self.authorization = "Basic " + basic
        if token:
            self.authorization = "Bearer " + token

    async def request(
        self, path, *, method="GET", query=None, body=None, headers=None, format="json", max_bytes=None
    ):
        limit = self.max_bytes if max_bytes is None else _limit(max_bytes, 128 * MIB, "max_bytes")
        if (
            not isinstance(path, str)
            or re.search(r"[\\:?#%\x00-\x20]", path)
            or any(part in ("", ".", "..") for part in path.split("/"))
        ):
            raise ValueError("Недопустимый путь сервиса 1С.")
        url = self.base_url + quote(path, safe="/$(),'")
        if query:
            url += "?" + urlencode(query, quote_via=quote)
        request_headers = dict(headers or {})
        request_headers.update(
            {
                "Accept": "application/xml" if format == "text" else "application/json",
                "Accept-Encoding": "identity",
            }
        )
        if self.authorization:
            request_headers["Authorization"] = self.authorization
        payload = None
        if body is not None:
            payload = _encode_json(body).encode("utf-8")
            if len(payload) > 4 * MIB:
                raise ValueError("Размер запроса превышает 4 МиБ.")
            request_headers["Content-Type"] = "application/json; charset=utf-8"
        uncertainty = UNKNOWN_WRITE if method != "GET" else ""
        status = None
        try:
            async with asyncio.timeout(self.timeout):
                # A fresh client guarantees that cookies cannot migrate between requests. No retries.
                async with httpx.AsyncClient(
                    verify=self.ssl_context, trust_env=False, follow_redirects=False, timeout=self.timeout
                ) as client:
                    async with client.stream(
                        method, url, headers=request_headers, content=payload
                    ) as response:
                        status = response.status_code
                        if not 200 <= status <= 299:
                            if status in (403, 422) and method == "POST" and path == "change":
                                try:
                                    error = json.loads(await _read_bytes(response, 64 * 1024, ""))
                                except (ValueError, RecursionError, TransportError):
                                    error = None
                                code = error.get("error") if isinstance(error, dict) else None
                                if status == 422 and code == "posted_document":
                                    raise PostedDocumentError()
                                if isinstance(code, str) and code in _CHANGE_REFUSALS:
                                    raise ChangeRefusedError(code, status, _user_messages(error.get("messages")))
                            detail = {
                                401: "Проверьте авторизацию.",
                                403: "Недостаточно прав.",
                                404: "Объект или публикация не найдены.",
                                412: "Версия объекта изменилась. Прочитайте объект повторно.",
                            }.get(
                                status,
                                "Перенаправление запрещено." if status < 400 else "Проверьте журнал 1С.",
                            )
                            raise TransportError(
                                f"1С HTTP {status}. {detail}" + (uncertainty if status >= 500 else ""), status
                            )
                        raw = await _read_bytes(response, limit, uncertainty)
                        reply_headers = response.headers
        except (httpx.HTTPError, TimeoutError, OSError):
            if status == 422:
                raise TransportError("1С отклонила запрос (HTTP 422).", 422) from None
            raise TransportError("Сетевая ошибка или таймаут при обращении к 1С." + uncertainty) from None
        try:
            text = raw.decode("utf-8-sig")
            if format == "text":
                data = text
            else:
                data = (
                    json.loads(
                        text,
                        parse_int=_read_integer,
                        parse_float=_read_fraction,
                        parse_constant=_invalid_json_constant,
                    )
                    if text.strip()
                    else None
                )
        except (ValueError, RecursionError):
            raise TransportError("Получен некорректный ответ 1С." + uncertainty) from None
        return {"status": status, "data": data, "headers": reply_headers}
