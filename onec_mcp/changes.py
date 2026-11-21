"""Preview, bind and consume a single data change in the current MCP process."""

import asyncio
import copy
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from uuid import uuid4

from .audit import AuditError
from .odata import PostedDocumentGuardUnavailable, PostingOperationsUnavailable
from .transport import ChangeRefusedError, PostedDocumentError

_METHODS = {
    "create": "create",
    "update": "update",
    "mark": "markDeletion",
    "delete": "remove",
    "post": "post",
    "unpost": "unpost",
}
_EXPECTED = {"markDeletion": {"DeletionMark": True}, "post": {"Posted": True}, "unpost": {"Posted": False}}
_OLD_EXTENSION = (
    "Документ проведён, а установленное расширение OneCMCP не умеет перепроводить его. "
    "Обновите расширение до версии 1.3.0 или новее."
)
# Platform behaviour confirmed on 8.3.27: operative posting moves the time of today's document to "now".
_OPERATIVE = (
    " Если для вида документа разрешено оперативное проведение и документ датирован текущим днём, "
    "1С проведёт его оперативно и, как при проведении из формы, установит время документа равным текущему."
)
_IDENTITY_FIELDS = ("Ref_Key", "Code", "Description", "Number", "Date", "Posted", "DeletionMark")
_UNSET = object()
_TTL = 300
_MAX_PENDING = 64
_MAX_PREVIEW = 96 * 1024


@dataclass(slots=True)
class _Pending:
    operation_id: str
    base: str
    action: str
    args: dict
    deadline: float

    @property
    def method(self):
        return _METHODS[self.action]


def _matches_submitted(actual, expected) -> bool:
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(_matches_submitted(a, e) for a, e in zip(actual, expected))
        )
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and _matches_submitted(actual[k], v) for k, v in expected.items()
        )
    # JSON distinguishes true/false from 1/0. Python equality alone does not.
    if isinstance(actual, bool) or isinstance(expected, bool):
        return actual is expected
    return actual == expected


async def _check_written(client, entry: _Pending, result: dict) -> dict:
    expected = _EXPECTED.get(entry.method) or entry.args["data"]
    key = entry.args.get("id") or result.get("id")
    if not key:
        return {"checked": False, "message": "Ответ 1С не содержит ключа для контрольного чтения."}
    try:
        read = await client.get(collection=entry.args["collection"], id=key, select=list(expected))
        actual = read["data"]
    except Exception:  # noqa: BLE001 - verification failure cannot undo a committed write.
        return {"checked": False, "message": "Запись принята. Контрольное чтение недоступно."}
    differences = [k for k, v in expected.items() if k not in actual or not _matches_submitted(actual[k], v)]
    return {"checked": True, "differences": differences}


def _table_diff(before, after):
    """1C replacement is positional. Report row positions, never infer an item identity."""
    changed = []
    for position, (old, new) in enumerate(zip(before, after), 1):
        fields = []
        for name in sorted(old.keys() | new.keys()):
            if name not in new and name in {"LineNumber", "Ref_Key"}:
                continue
            if name not in old or name not in new or not _matches_submitted(old[name], new[name]):
                fields.append(name)
        if fields:
            changed.append({"row": position, "fields": fields})
    return {
        "comparison": "position",
        "beforeCount": len(before),
        "afterCount": len(after),
        "addedRows": list(range(len(before) + 1, len(after) + 1)),
        "removedRows": list(range(len(after) + 1, len(before) + 1)),
        "changedRows": changed,
    }


def _posting_effect(operation, posted, reposts):
    """Describe what happens to register records. posted is None when the profile hides it."""
    if operation == "delete":
        if posted:
            raise ValueError(
                "Проведённый документ нельзя удалить физически. Сначала отмените проведение (unpost) "
                "или пометьте его на удаление (mark)."
            )
        return "Непроведённый документ будет удалён. Проведённый документ 1С откажется удалять."
    if operation == "unpost":
        if posted is False:
            raise ValueError("Документ не проведён. Отменять проведение не требуется.")
        return "Проведение будет отменено, движения документа по регистрам удалятся."
    if operation == "post":
        if posted:
            return "Документ уже проведён и будет перепроведён: движения регистров сформируются заново." + _OPERATIVE
        return "Документ будет проведён. При отказе обработчиков проведения ничего не изменится." + _OPERATIVE
    if posted and not reposts:
        raise ValueError(_OLD_EXTENSION)
    if posted is None and not reposts:
        return (
            "Если документ окажется проведён, установленное расширение отклонит операцию. "
            "Обновите OneCMCP до 1.3.0."
        )
    if operation == "mark":
        if posted:
            return (
                "Документ проведён. Проведение будет отменено, движения удалятся, "
                "документ будет помечен на удаление."
            )
        if posted is None:
            return "Если документ окажется проведён, 1С отменит проведение и удалит движения вместе с пометкой."
        return "Документ не проведён. Движения не затрагиваются."
    if posted:
        return (
            "Документ проведён. Изменение будет записано с перепроведением в одной транзакции: "
            "движения регистров пересчитаются. Если 1С откажет в проведении, документ и движения не изменятся."
            + _OPERATIVE
        )
    if posted is None:
        return "Если документ окажется проведён, 1С перепроведёт его вместе с изменением." + _OPERATIVE
    return "Документ не проведён. Изменение записывается без проведения."


def _preview_size(plan: dict) -> int:
    text = json.dumps(plan, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    # JSON.stringify escapes unpaired UTF-16 surrogates as six ASCII characters.
    return len(text.encode("utf-8", errors="backslashreplace"))


class ChangePlans:
    def __init__(self, resolve, audit):
        self.resolve = resolve
        self.audit = audit
        self._entries: dict[str, _Pending] = {}
        self._active_objects: set[tuple[str, str, str]] = set()

    async def prepare(
        self,
        *,
        base,
        operation,
        collection,
        data=_UNSET,
        id=_UNSET,
        version=_UNSET,
        replaceTableSections=_UNSET,
    ) -> dict:
        session = self.resolve(base)
        client, profile = session.client, session.profile
        method = _METHODS.get(operation)
        if method is None:
            raise ValueError("Неизвестная операция.")
        allowed = profile["writeCollections"]
        if collection not in allowed and "*" not in allowed:
            raise ValueError("Запись для этой коллекции отключена в профиле " + base)
        if operation == "delete" and not profile.get("physicalDelete"):
            raise ValueError("Физическое удаление выключено в профиле " + base)

        create = operation == "create"
        has_fields = operation in ("update", "create")
        if (create and (id is not _UNSET or version is not _UNSET)) or (
            not create and (not isinstance(id, str) or not id or not isinstance(version, str) or not version)
        ):
            raise ValueError(
                "Для существующего объекта укажите id и version из onec_get. Для создания опустите оба."
            )
        if has_fields != (data is not _UNSET) or (has_fields and (not isinstance(data, dict) or not data)):
            raise ValueError("Операции create и update требуют непустой объект data.")
        if replaceTableSections is not _UNSET and (
            operation != "update" or not isinstance(replaceTableSections, bool)
        ):
            raise ValueError("replaceTableSections относится только к update.")

        args = {"collection": collection}
        plan = {"base": base, "operation": operation, "collection": collection}
        if not create:
            args.update(id=id, etag=version)
            plan.update(id=id, version=version)
        if has_fields:
            data = copy.deepcopy(data)
            args["data"] = data
        if replaceTableSections is True:
            args["replaceTableSections"] = True
        if operation == "delete":
            args["confirmId"] = id
        preview = await client.validate_write(method, **args)
        reposts = isinstance(preview, dict) and preview.get("repostsPostedDocuments") is True

        field_limits = getattr(session, "field_limits", None)
        if has_fields and field_limits and any(isinstance(v, str) for v in data.values()):
            limits = await field_limits(collection)
            if limits is None:
                plan["fieldLimits"] = (
                    "Расширение не вернуло ограничения длины. "
                    "После применения сверьте результат проверки полей."
                )
            else:
                for name, value in data.items():
                    max_length = limits.get(name)
                    if isinstance(value, str) and type(max_length) is int and 0 < max_length < 2**53:
                        units = len(value.encode("utf-16-le", "surrogatepass")) // 2
                        if units > max_length:
                            raise ValueError(
                                f"Поле {name} допускает до {max_length} кодовых единиц UTF-16. Получено {units}."
                            )
        document = collection.startswith("Document_")
        posted = None
        if not create:
            names = None
            if operation != "update" or document:
                schema = await client.describe(collection=collection)
                names = {field["name"] for field in schema["fields"]}
            if operation == "update":
                fields = list(data)
            else:
                fields = [f for f in _IDENTITY_FIELDS if f in names]
            # Posted is read only where the profile shows it. Otherwise 1C decides under its lock.
            select = fields + (["Posted"] if document and "Posted" in names and "Posted" not in fields else [])
            current = await client.get(collection=collection, id=id, select=select)
            if current.get("etag") != version:
                raise ValueError("Изменилась версия объекта. Перечитайте его через onec_get.")
            row = current["data"]
            if document and isinstance(row.get("Posted"), bool):
                posted = row["Posted"]
            if operation == "update" and any(f not in row for f in fields):
                raise ValueError(
                    "База не вернула текущее значение одного из изменяемых полей. Предпросмотр невозможен."
                )
            plan["before"] = {f: row[f] for f in fields if f in row}
        if document and not create:
            plan["posting"] = _posting_effect(operation, posted, reposts)
        if has_fields:
            plan["after"] = data
        elif operation == "mark":
            plan.update(
                after={"DeletionMark": True},
                effect="Изменится поле DeletionMark. Каскадная пометка подчинённых объектов не выполняется.",
            )
        elif operation in ("post", "unpost"):
            plan["after"] = {"Posted": operation == "post"}
        else:
            plan["effect"] = "Физическое удаление объекта в транзакции 1С."
        if replaceTableSections is True:
            plan["tableSections"] = "Передаваемые табличные части будут заменены целиком."
        if has_fields:
            table_diffs = {}
            for field, submitted in data.items():
                if isinstance(submitted, list):
                    old = plan.get("before", {}).get(field, [])
                    if not isinstance(old, list) or not all(
                        isinstance(row, dict) for rows in (old, submitted) for row in rows
                    ):
                        raise ValueError("Невозможно сравнить строки табличной части.")
                    table_diffs[field] = _table_diff(old, submitted)
            if table_diffs:
                plan["tableDiff"] = table_diffs
        # Explicit null conveys an object that does not exist before/after the operation.
        plan.setdefault("before", None)
        plan.setdefault("after", None)
        if _preview_size(plan) > _MAX_PREVIEW:
            raise ValueError("Предпросмотр больше 96 КиБ. Сократите состав изменения.")

        now = monotonic()
        self._entries = {t: e for t, e in self._entries.items() if e.deadline > now}
        if len(self._entries) >= _MAX_PENDING:
            raise ValueError("Достигнут лимит ожидающих планов. Срок действия плана — 5 минут.")
        ticket = str(uuid4())
        operation_id = str(uuid4())
        self._entries[ticket] = _Pending(operation_id, base, operation, copy.deepcopy(args), now + _TTL)
        expires = datetime.now(UTC) + timedelta(seconds=_TTL)
        return {
            **plan,
            "operationId": operation_id,
            "ticket": ticket,
            "expiresAt": expires.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }

    async def apply(self, *, base, ticket) -> dict:
        entry = self._entries.get(ticket)
        if entry is None or entry.deadline <= monotonic():
            self._entries.pop(ticket, None)
            raise ValueError("План не найден, уже использован или истёк.")
        if entry.base != base:
            raise ValueError("План создан для другой базы.")
        # Consume before the first yield, including on local audit failure or cancellation.
        del self._entries[ticket]
        session = self.resolve(base)
        client = session.client
        args = entry.args
        collection, object_id = args["collection"], args.get("id")
        operation_id = entry.operation_id
        try:
            self.audit.start(
                operation_id, base, entry.action, collection, object_id, getattr(session, "audit_user", None)
            )
        except AuditError:
            raise ValueError(
                f"Операция {operation_id}: журнал недоступен. Запись в 1С не отправлена."
            ) from None

        key = (base, collection, object_id.lower()) if object_id else None
        reserved = False
        dispatched = False
        outcome = "rejected"
        status = None
        note = "Проверка перед записью не пройдена."
        try:
            if key is not None:
                if key in self._active_objects:
                    outcome, note = "conflict", "Идёт изменение этого объекта."
                    raise ValueError
                self._active_objects.add(key)
                reserved = True
            if object_id:
                current = await client.get(collection=collection, id=object_id, select=["DataVersion"])
                if current.get("etag") != args["etag"]:
                    outcome, note = "conflict", "Версия объекта изменилась после предпросмотра."
                    raise ValueError
            dispatched = True
            result = await getattr(client, entry.method)(**args)
            outcome = "committed"
            status = result["status"]
            object_id = object_id or result.get("id")
        except (asyncio.CancelledError, Exception) as error:
            status = getattr(error, "status", None)
            if isinstance(
                error, (PostedDocumentError, PostedDocumentGuardUnavailable, PostingOperationsUnavailable)
            ):
                outcome, note = "rejected", str(error)
            elif isinstance(error, ChangeRefusedError):
                outcome, note = "rejected", str(error)
                if error.messages and session.profile.get("writeMessages", True) is not False:
                    note += " Сообщения 1С: " + " | ".join(error.messages)
            elif status in (409, 412):
                outcome, note = "conflict", "Конфликт версии или блокировки объекта в 1С."
            elif dispatched:
                if status in (400, 401, 403, 404, 405, 413, 415, 422):
                    outcome, note = "rejected", f"1С отклонила запись (HTTP {status})."
                else:
                    outcome, note = (
                        "unknown",
                        "Результат записи неизвестен. Проверьте состояние объекта в 1С.",
                    )
            if isinstance(error, asyncio.CancelledError):
                raise
            raise ValueError(f"Операция {operation_id}: {note} План закрыт.") from None
        finally:
            if reserved:
                self._active_objects.discard(key)
            try:
                self.audit.finish(operation_id, outcome, object_id=object_id, status=status)
            except AuditError:
                # The durable 'started' row intentionally remains indeterminate.
                if outcome == "committed":
                    note = "1С приняла запись, но результат не удалось сохранить в журнале."
                elif dispatched:
                    note = "Результат не удалось сохранить в журнале. Проверьте состояние объекта в 1С."
                else:
                    note = "Запись в 1С не отправлена, результат не удалось сохранить в журнале."
                raise ValueError(f"Операция {operation_id}: {note} План закрыт.") from None
        output = {
            "operationId": operation_id,
            "base": base,
            "collection": collection,
            "result": {"status": status, "etag": result.get("etag")},
        }
        if object_id:
            output["id"] = object_id
        for flag in ("posted", "reposted"):
            if flag in result:
                output["result"][flag] = result[flag]
        if entry.method != "remove":
            output["verification"] = await _check_written(client, entry, result)
        return output
