"""Preview, bind and consume a single data change in the current MCP process."""

import copy
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic
from uuid import uuid4

_METHODS = {"create": "create", "update": "update", "mark": "markDeletion", "delete": "remove"}
_IDENTITY_FIELDS = ("Ref_Key", "Code", "Description", "Number", "Date", "Posted", "DeletionMark")
_UNSET = object()
_TTL = 300
_MAX_PENDING = 64
_MAX_PREVIEW = 96 * 1024


@dataclass(slots=True)
class _Pending:
    base: str
    method: str
    args: dict
    deadline: float


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
    expected = {"DeletionMark": True} if entry.method == "markDeletion" else entry.args["data"]
    actual = result.get("data")
    if not isinstance(actual, dict) or not expected.keys() <= actual.keys():
        key = entry.args.get("id") or (actual.get("Ref_Key") if isinstance(actual, dict) else None)
        if not key:
            return {"checked": False, "message": "Ответ 1С не содержит ключа для контрольного чтения."}
        try:
            read = await client.get(collection=entry.args["collection"], id=key, select=list(expected))
            actual = read["data"]
        except Exception as error:
            return {"checked": False, "message": "Запись принята. Контрольное чтение: " + str(error)}
    differences = [k for k, v in expected.items() if k not in actual or not _matches_submitted(actual[k], v)]
    return {"checked": True, "differences": differences}


def _preview_size(plan: dict) -> int:
    text = json.dumps(plan, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    # JSON.stringify escapes unpaired UTF-16 surrogates as six ASCII characters.
    return len(text.encode("utf-8", errors="backslashreplace"))


class ChangePlans:
    def __init__(self, resolve):
        self.resolve = resolve
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
        await client.validate_write(method, **args)

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
        if not create:
            if operation == "update":
                fields = list(data)
            else:
                schema = await client.describe(collection=collection)
                names = {field["name"] for field in schema["fields"]}
                fields = [f for f in _IDENTITY_FIELDS if f in names]
            current = await client.get(collection=collection, id=id, select=fields)
            if current.get("etag") != version:
                raise ValueError("Изменилась версия объекта. Перечитайте его через onec_get.")
            row = current["data"]
            if operation == "update" and any(f not in row for f in fields):
                raise ValueError(
                    "База не вернула текущее значение одного из изменяемых полей. Предпросмотр невозможен."
                )
            plan["before"] = {f: row[f] for f in fields if f in row}
        if has_fields:
            plan["after"] = data
        elif operation == "mark":
            plan.update(
                after={"DeletionMark": True},
                effect=(
                    "Изменится поле DeletionMark. Каскадная пометка и отмена проведения командой формы здесь не выполняются."
                ),
            )
        else:
            plan["effect"] = "Физическое удаление объекта через OData DELETE."
        if replaceTableSections is True:
            plan["tableSections"] = "Передаваемые табличные части будут заменены целиком."
        if _preview_size(plan) > _MAX_PREVIEW:
            raise ValueError("Предпросмотр больше 96 КиБ. Сократите состав изменения.")

        now = monotonic()
        self._entries = {t: e for t, e in self._entries.items() if e.deadline > now}
        if len(self._entries) >= _MAX_PENDING:
            raise ValueError("Достигнут лимит ожидающих планов. Срок действия плана — 5 минут.")
        ticket = str(uuid4())
        self._entries[ticket] = _Pending(base, method, copy.deepcopy(args), now + _TTL)
        expires = datetime.now(timezone.utc) + timedelta(seconds=_TTL)
        return {
            **plan,
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
        # Consume the ticket and reserve the object before yielding control to another MCP call.
        del self._entries[ticket]
        client = self.resolve(base).client
        args = entry.args
        collection, object_id = args["collection"], args.get("id")
        key = (base, collection, object_id.lower()) if object_id else None
        if key is not None:
            if key in self._active_objects:
                raise ValueError("Идёт изменение этого объекта. Перечитайте его и подготовьте новый план.")
            self._active_objects.add(key)
        try:
            if object_id:
                current = await client.get(collection=collection, id=object_id, select=["DataVersion"])
                if current.get("etag") != args["etag"]:
                    raise ValueError("Версия объекта изменилась после предпросмотра. Запись остановлена.")
            result = await getattr(client, entry.method)(**args)
            output = {"base": base, "collection": collection, "result": result}
            row = result.get("data")
            if object_id:
                output["id"] = object_id
            elif isinstance(row, dict) and "Ref_Key" in row:
                output["id"] = row["Ref_Key"]
            if entry.method != "remove":
                output["verification"] = await _check_written(client, entry, result)
            return output
        except Exception as error:
            raise ValueError(
                str(error) + " План закрыт. Перед повторной операцией проверьте состояние объекта в 1С."
            ) from error
        finally:
            if key is not None:
                self._active_objects.discard(key)
