"""1C standard OData v3 discovery, structured queries and conditional data changes."""

import asyncio
import base64
import binascii
import re
from copy import deepcopy
from decimal import Decimal
from io import StringIO
from time import monotonic
from xml.etree import ElementTree as ET

from .transport import MIB

EDMX = "http://schemas.microsoft.com/ado/2007/06/edmx"
EDM = {
    "http://schemas.microsoft.com/ado/" + version + "/edm"
    for version in ("2006/04", "2007/05", "2008/09", "2009/11")
}
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I | re.ASCII)
NULL_GUID = "00000000-0000-0000-0000-000000000000"
RESERVED = {"Ref_Key", "DataVersion", "DeletionMark", "Posted", "Predefined", "PredefinedDataName"}
INT_TYPES = {"Edm.Byte", "Edm.SByte", "Edm.Int16", "Edm.Int32", "Edm.Int64"}
FLOAT_TYPES = {"Edm.Decimal", "Edm.Double", "Edm.Single"}
NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
COLLECTION = re.compile(r"Collection\(([^)]+)\)")
DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,7})?"
METHODS = {"create": "POST", "update": "PATCH", "markDeletion": "PATCH", "remove": "DELETE"}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _identifier(name):
    _require(isinstance(name, str) and name.isidentifier(), "Некорректное имя в схеме OData.")
    return name


def _split_tag(tag):
    namespace, _, local = tag.rpartition("}")
    return namespace.lstrip("{"), local


class _Schema:
    def __init__(self, xml):
        _require(
            isinstance(xml, str) and "<!DOCTYPE" not in xml and "\x00" not in xml,
            "DTD и двоичные данные в описании OData запрещены.",
        )
        self.sets = {}
        self.types = {}
        self.aliases = {}
        stack = []
        namespace = entity = None
        saw_schema = False
        try:
            for event, item in ET.iterparse(StringIO(xml), events=("start", "end")):
                uri, tag = _split_tag(item.tag)
                if event == "end":
                    stack.pop()
                    if uri in EDM and tag in ("ComplexType", "EntityType"):
                        entity = None
                    if uri in EDM and tag == "Schema":
                        namespace = None
                    item.clear()
                    continue
                parent = stack[-1] if stack else None
                if parent is None:
                    _require((uri, tag) == (EDMX, "Edmx"), "Ожидалось описание EDMX OData v3.")
                stack.append(tag)
                _require(len(stack) <= 128, "Слишком большая глубина EDMX.")
                if uri not in EDM:
                    continue
                if tag == "Schema" and parent == "DataServices":
                    namespace = item.get("Namespace")
                    _require(namespace, "В EDMX не задано пространство имён.")
                    saw_schema = True
                    if item.get("Alias"):
                        self.aliases[item.get("Alias")] = namespace
                elif tag in ("ComplexType", "EntityType") and parent == "Schema" and namespace:
                    qualified = namespace + "." + _identifier(item.get("Name"))
                    _require(qualified not in self.types, "В EDMX повторяется тип.")
                    entity = {"kind": tag, "keys": [], "fields": {}}
                    self.types[qualified] = entity
                elif tag == "PropertyRef" and parent == "Key" and entity is not None:
                    entity["keys"].append(_identifier(item.get("Name")))
                elif tag == "Property" and parent in ("ComplexType", "EntityType") and entity is not None:
                    name = _identifier(item.get("Name"))
                    _require(item.get("Type") and name not in entity["fields"], "Некорректное поле EDMX.")
                    field = {
                        "name": name,
                        "type": item.get("Type"),
                        "nullable": item.get("Nullable") != "false",
                    }
                    for source, target in (
                        ("MaxLength", "maxLength"),
                        ("Precision", "precision"),
                        ("Scale", "scale"),
                    ):
                        if source in item.attrib:
                            field[target] = item.get(source)
                    entity["fields"][name] = field
                elif tag == "EntitySet" and parent == "EntityContainer" and namespace:
                    name = _identifier(item.get("Name"))
                    _require(name not in self.sets, "В EDMX повторяется набор.")
                    self.sets[name] = {"name": name, "entityType": item.get("EntityType")}
        except ET.ParseError:
            raise ValueError("Некорректное описание EDMX OData.") from None
        _require(saw_schema, "В публикации отсутствует схема OData v3.")
        for item in self.sets.values():
            entity = self.resolve(item["entityType"])
            _require(entity and entity["kind"] == "EntityType", "Неизвестный тип набора в EDMX.")
            item["keys"] = entity["keys"]
            item["refObject"] = (
                item["name"].startswith(("Document_", "Catalog_"))
                and entity["keys"] == ["Ref_Key"]
                and entity["fields"].get("Ref_Key", {}).get("type") == "Edm.Guid"
            )

    def resolve(self, name):
        if not isinstance(name, str):
            return None
        prefix, sep, local = name.rpartition(".")
        return self.types.get(self.aliases.get(prefix, prefix) + sep + local)


def _page(top, skip):
    _require(type(top) is int and 1 <= top <= 100, "top должен быть от 1 до 100.")
    _require(type(skip) is int and 0 <= skip <= 2147483647, "Недопустимое значение skip.")


def _array(value, maximum, name):
    _require(
        isinstance(value, list) and len(value) <= maximum,
        f"{name} должен быть массивом до {maximum} элементов.",
    )
    return value


def _guid(value):
    _require(isinstance(value, str) and UUID.fullmatch(value), "Требуется UUID ссылки 1С.")
    return value


def _version(value):
    _require(isinstance(value, str) and 0 < len(value) <= 256, "Требуется исходная версия DataVersion.")
    try:
        base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Требуется исходная версия DataVersion.") from None


def _field(entity, name):
    _require(isinstance(name, str) and name in entity["fields"], "Неизвестный реквизит OData.")
    return entity["fields"][name]


def _project(entity, select, extras=()):
    if select is None:
        return None
    items = [_field(entity, name)["name"] for name in _array(select, 128, "select")]
    _require(items, "select должен содержать хотя бы один реквизит.")
    items.extend(name for name in extras if name in entity["fields"])
    return ",".join(dict.fromkeys(items))


def _number(kind, value):
    _require(type(value) in (str, int, float, Decimal), "Передайте число или десятичную строку.")
    literal = str(value)
    _require(len(literal) <= 128 and NUMBER.fullmatch(literal), "Требуется конечное десятичное число.")
    if kind in INT_TYPES:
        _require(re.fullmatch(r"-?[0-9]+", literal), "Для поля требуется целое число.")
    return literal


def _literal(kind, value):
    if value is None:
        return "null"
    if kind == "Edm.Guid":
        return "guid'" + _guid(value) + "'"
    if kind == "Edm.Boolean":
        _require(type(value) is bool, "Требуется логическое значение.")
        return "true" if value else "false"
    if kind in INT_TYPES or kind in FLOAT_TYPES:
        suffix = {"Edm.Int64": "L", "Edm.Decimal": "M", "Edm.Double": "D", "Edm.Single": "F"}.get(kind, "")
        return _number(kind, value) + suffix
    if kind in ("Edm.DateTime", "Edm.DateTimeOffset"):
        offset = kind == "Edm.DateTimeOffset"
        pattern = DATE + (r"(?:Z|[+-][0-9]{2}:[0-9]{2})" if offset else "")
        _require(isinstance(value, str) and re.fullmatch(pattern, value), "Требуется дата-время ISO 8601.")
        return ("datetimeoffset'" if offset else "datetime'") + value + "'"
    _require(
        kind == "Edm.String" and isinstance(value, str) and len(value) <= 2048, "Недопустимое условие отбора."
    )
    return "'" + value.replace("'", "''") + "'"


def _fields(model, entity, data, *, sections=False, depth=0):
    _require(
        depth <= 12 and isinstance(data, dict) and 1 <= len(data) <= 512, "Передайте от 1 до 512 реквизитов."
    )
    encoded = {}
    for name, value in data.items():
        field = _field(entity, name)
        _require(depth != 0 or name not in RESERVED, "Изменение системного поля запрещено: " + name)
        kind = field["type"]
        table = COLLECTION.fullmatch(kind)
        if table:
            _require(sections, "Для полной замены табличной части требуется replaceTableSections=true.")
            row = model.resolve(table.group(1))
            _require(row and row["kind"] == "ComplexType", "Неподдерживаемый тип табличной части.")
            encoded[name + "@odata.type"] = kind
            value = [_fields(model, row, line, depth=depth + 1) for line in _array(value, 5000, name)]
        elif value is None:
            _require(field["nullable"], "Поле не допускает null: " + name)
        elif kind == "Edm.Guid":
            _guid(value)
        elif kind in INT_TYPES or kind in FLOAT_TYPES:
            literal = _number(kind, value)
            value = literal if kind in ("Edm.Int64", "Edm.Decimal") else Decimal(literal)
        elif kind == "Edm.Boolean":
            _require(type(value) is bool, "Для поля требуется true/false: " + name)
        elif (item := model.resolve(kind)) and item["kind"] == "ComplexType":
            value = _fields(model, item, value, depth=depth + 1)
        else:
            _require(isinstance(value, str), "Для поля требуется строка: " + name)
            limit = field.get("maxLength", "")
            if kind == "Edm.String" and re.fullmatch(r"[0-9]+", limit):
                size = len(value.encode("utf-16-le")) // 2
                _require(size <= int(limit), f"Поле {name} допускает {limit} единиц UTF-16, передано {size}.")
        encoded[name] = value
    return encoded


class ODataClient:
    def __init__(self, profile, transport):
        self.profile = profile
        self.transport = transport
        self._model = None
        self._expires = 0
        self._lock = asyncio.Lock()

    async def _schema(self):
        async with self._lock:
            if self._model is None or monotonic() >= self._expires:
                size = self.profile.get("maxMetadataMiB", self.profile.get("maxResponseMiB", 32)) * MIB
                response = await self.transport.request("$metadata", format="text", max_bytes=size)
                self._model = _Schema(response["data"])
                self._expires = monotonic() + 60
        return self._model

    async def _lookup(self, collection, ref=False):
        _identifier(collection)
        model = await self._schema()
        resource = model.sets.get(collection)
        _require(resource, "Набор отсутствует в опубликованном интерфейсе OData.")
        _require(
            not ref or resource["refObject"], "Операция требует справочник или документ с ключом Ref_Key."
        )
        return model, resource, model.resolve(resource["entityType"])

    async def collections(self, *, search="", top=25, skip=0):
        _page(top, skip)
        _require(isinstance(search, str) and len(search) <= 256, "Недопустимая строка поиска.")
        model = await self._schema()
        needle = search.casefold()
        names = sorted((name for name in model.sets if needle in name.casefold()), key=str.casefold)
        return {
            "collections": [deepcopy(model.sets[n]) for n in names[skip : skip + top]],
            "total": len(names),
            "skip": skip,
            "top": top,
        }

    async def describe(self, *, collection):
        model, resource, entity = await self._lookup(collection)
        fields = []
        for field in entity["fields"].values():
            field = field.copy()
            table = COLLECTION.fullmatch(field["type"])
            if table and (row := model.resolve(table.group(1))):
                field["rowFields"] = list(row["fields"].values())
            fields.append(field)
        return deepcopy({**resource, "fields": fields})

    async def list(self, *, collection, top=25, skip=0, select=None, filter=None, orderBy=None):
        _page(top, skip)
        _, _, entity = await self._lookup(collection)
        query = {"$format": "json", "$top": top, "$skip": skip, "allowedOnly": "true"}
        if fields := _project(entity, select):
            query["$select"] = fields
        conditions = []
        for item in _array([] if filter is None else filter, 16, "filter"):
            _require(isinstance(item, dict), "Недопустимое условие отбора.")
            prop = _field(entity, item.get("field"))
            op = item.get("op")
            _require(
                op in ("eq", "ne", "gt", "ge", "lt", "le") and "value" in item, "Недопустимое условие отбора."
            )
            conditions.append(f"{prop['name']} {op} " + _literal(prop["type"], item["value"]))
        if conditions:
            query["$filter"] = " and ".join(conditions)
        order = {}
        for item in _array([] if orderBy is None else orderBy, 16, "orderBy"):
            _require(isinstance(item, dict), "Недопустимая сортировка.")
            field = _field(entity, item.get("field"))
            direction = item.get("direction", "asc")
            _require(direction in ("asc", "desc"), "Укажите asc или desc.")
            order[field["name"]] = direction
        for key in entity["keys"]:
            order.setdefault(key, "asc")
        if order:
            query["$orderby"] = ",".join(k + " " + d for k, d in order.items())
        response = await self.transport.request(collection, query=query)
        data = response["data"]
        _require(isinstance(data, dict), "Некорректный ответ списка OData.")
        envelope = data.get("d")
        rows = (
            data.get("value")
            if "value" in data
            else envelope.get("results")
            if isinstance(envelope, dict)
            else None
        )
        _require(
            isinstance(rows, list) and len(rows) <= top and all(isinstance(r, dict) for r in rows),
            "Некорректный ответ списка OData.",
        )
        more = (
            len(rows) == top
            or data.get("odata.nextLink")
            or (isinstance(envelope, dict) and envelope.get("__next"))
        )
        return {
            "data": rows,
            "top": top,
            "skip": skip,
            "nextSkip": skip + len(rows) if rows and more else None,
        }

    async def get(self, *, collection, id, select=None):
        _guid(id)
        _, _, entity = await self._lookup(collection, ref=True)
        query = {"$format": "json"}
        if fields := _project(entity, select, ["DataVersion"]):
            query["$select"] = fields
        response = await self.transport.request(f"{collection}(guid'{id}')", query=query)
        data = response["data"]
        row = data.get("d", data) if isinstance(data, dict) else None
        _require(isinstance(row, dict), "Некорректный ответ объекта OData.")
        return {"data": row, "etag": row.get("DataVersion")}

    async def _prepare(
        self, method, *, collection, id=None, etag=None, data=None, confirmId=None, replaceTableSections=False
    ):
        allowed = self.profile.get("writeCollections", [])
        _require(
            isinstance(allowed, list) and (collection in allowed or "*" in allowed),
            "Запись для набора отключена.",
        )
        _require(method in METHODS, "Неизвестная операция.")
        creating = method == "create"
        if not creating:
            _require(_guid(id) != NULL_GUID, "Пустая ссылка недопустима для записи.")
            _version(etag)
        if method == "remove":
            _require(self.profile.get("physicalDelete") is True, "Физическое удаление отключено.")
            _require(
                isinstance(confirmId, str) and confirmId.lower() == id.lower(),
                "Подтвердите точный UUID удаления.",
            )
        model, _, entity = await self._lookup(collection, ref=True)
        if not creating:
            _field(entity, "DataVersion")
        body = None
        if method == "markDeletion":
            _require(
                _field(entity, "DeletionMark")["type"] == "Edm.Boolean", "Поле пометки удаления недоступно."
            )
            body = {"DeletionMark": True}
        if creating or method == "update":
            body = _fields(model, entity, data, sections=creating or replaceTableSections is True)
            if creating:
                body = {"Ref_Key": NULL_GUID, **body}
        preview = {
            "method": method,
            "collection": collection,
            "fields": ["DeletionMark"] if method == "markDeletion" else list(data or {}),
        }
        if id:
            preview["id"] = id
        return body, preview

    async def validate_write(self, method, **args):
        _, preview = await self._prepare(method, **args)
        return preview

    async def _mutate(self, operation, **args):
        body, _ = await self._prepare(operation, **args)
        path = args["collection"]
        headers = {}
        if operation != "create":
            path += "(guid'" + args["id"] + "')"
            headers["If-Match"] = args["etag"]
        response = await self.transport.request(
            path, method=METHODS[operation], headers=headers, query={"$format": "json"}, body=body
        )
        data = response["data"]
        if isinstance(data, dict):
            data = data.get("d", data)
        return {
            "status": response["status"],
            "data": data,
            "etag": data.get("DataVersion") if isinstance(data, dict) else None,
        }

    async def create(self, **args):
        return await self._mutate("create", **args)

    async def update(self, **args):
        return await self._mutate("update", **args)

    async def markDeletion(self, **args):
        return await self._mutate("markDeletion", **args)

    async def remove(self, **args):
        return await self._mutate("remove", **args)
