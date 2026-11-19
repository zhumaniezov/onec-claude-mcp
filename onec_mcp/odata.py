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

from .profiles import can_read_collection
from .transport import MIB, TransportError

EDMX = "http://schemas.microsoft.com/ado/2007/06/edmx"
EDM = {
    "http://schemas.microsoft.com/ado/" + version + "/edm"
    for version in ("2006/04", "2007/05", "2008/09", "2009/11")
}
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE | re.ASCII)
NULL_GUID = "00000000-0000-0000-0000-000000000000"
RESERVED = {
    "Ref_Key",
    "DataVersion",
    "DeletionMark",
    "Posted",
    "Predefined",
    "PredefinedDataName",
}
INT_TYPES = {"Edm.Byte", "Edm.SByte", "Edm.Int16", "Edm.Int32", "Edm.Int64"}
FLOAT_TYPES = {"Edm.Decimal", "Edm.Double", "Edm.Single"}
NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
COLLECTION = re.compile(r"Collection\(([^)]+)\)")
DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,7})?"
WRITE_METHODS = {"create", "update", "markDeletion", "remove"}


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
                    _require(
                        (uri, tag) == (EDMX, "Edmx"),
                        "Ожидалось описание EDMX OData v3.",
                    )
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
                    _require(
                        item.get("Type") and name not in entity["fields"],
                        "Некорректное поле EDMX.",
                    )
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
                    self.sets[name] = {
                        "name": name,
                        "entityType": item.get("EntityType"),
                    }
        except ET.ParseError:
            raise ValueError("Некорректное описание EDMX OData.") from None
        _require(saw_schema, "В публикации отсутствует схема OData v3.")
        for item in self.sets.values():
            entity = self.resolve(item["entityType"])
            _require(
                entity and entity["kind"] == "EntityType",
                "Неизвестный тип набора в EDMX.",
            )
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
    _require(
        isinstance(value, str) and 0 < len(value) <= 256,
        "Требуется исходная версия DataVersion.",
    )
    try:
        base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Требуется исходная версия DataVersion.") from None


def _field(entity, name):
    _require(
        isinstance(name, str) and name in entity["fields"],
        "Неизвестный реквизит OData.",
    )
    return entity["fields"][name]


class _ReadView:
    """One EDMX entity intersected with its profile policy. None denotes all columns of a property."""

    def __init__(self, profile, collection, model, entity):
        self.model = model
        self.entity = entity
        rules = profile.get("readFields", {}).get(collection)
        self.restricted = rules is not None and rules != ["*"]
        self.allowed = {}
        if not self.restricted:
            self.allowed = dict.fromkeys(entity["fields"])
            return
        for rule in rules:
            name, slash, column = rule.partition("/")
            field = entity["fields"].get(name)
            if field is None:
                continue
            if not slash:
                self.allowed[name] = None
                continue
            row = self.row_type(field)
            if row and column in row["fields"] and self.allowed.get(name, {}) is not None:
                self.allowed.setdefault(name, {})[column] = None

    def row_type(self, field):
        table = COLLECTION.fullmatch(field["type"])
        return self.model.resolve(table.group(1)) if table else None

    def visible(self, name):
        _require(
            isinstance(name, str) and name in self.allowed,
            "Реквизит недоступен для чтения в этом профиле.",
        )
        return self.entity["fields"][name]

    def comparable(self, name):
        field = self.visible(name)
        _require(
            not COLLECTION.fullmatch(field["type"]) and self.model.resolve(field["type"]) is None,
            "Сортировка и отбор доступны только по простым полям.",
        )
        return field

    def projection(self, select, *, version=False):
        if select is None:
            fields = self.allowed.copy()
        else:
            fields = {}
            for path in _array(select, 128, "select"):
                _require(isinstance(path, str), "Некорректное имя поля.")
                name, slash, column = path.partition("/")
                field = self.visible(name)
                allowed = self.allowed[name]
                if not slash:
                    fields[name] = allowed
                else:
                    row = self.row_type(field)
                    _require(
                        row and column in row["fields"] and (allowed is None or column in allowed),
                        "Колонка табличной части недоступна.",
                    )
                    if name not in fields:
                        fields[name] = {}
                    if fields[name] is not None:
                        # Copy any dictionary inherited from the profile before adding a subfield.
                        fields[name] = {**fields[name], column: None}
            _require(fields, "select должен содержать хотя бы один реквизит.")
        if version and "DataVersion" in self.allowed:
            fields["DataVersion"] = None
        _require(fields, "У набора нет разрешённых опубликованных полей.")
        wire = [
            path
            for name, columns in fields.items()
            for path in ([name] if columns is None else [name + "/" + c for c in columns])
        ]
        return fields, ",".join(wire) if self.restricted or select is not None else None

    def summary(self, resource):
        return {**resource, "keys": [k for k in resource["keys"] if k in self.allowed]}

    def trim(self, row, projection):
        return self._trim_fields(row, self.entity, projection, 0)

    def _trim_fields(self, record, entity, projection, depth):
        _require(
            isinstance(record, dict) and depth <= 12,
            "Некорректный ответ объекта OData.",
        )
        result = {}
        for name, columns in projection.items():
            if name not in record:
                continue
            field = entity["fields"][name]
            value = record[name]
            if isinstance(value, (dict, list)):
                row_type = self.row_type(field)
                nested = self.model.resolve(field["type"])
                if row_type and isinstance(value, list):
                    schema = columns if columns is not None else dict.fromkeys(row_type["fields"])
                    value = [self._trim_fields(line, row_type, schema, depth + 1) for line in value]
                elif nested and isinstance(value, dict):
                    value = self._trim_fields(value, nested, dict.fromkeys(nested["fields"]), depth + 1)
                elif COLLECTION.fullmatch(field["type"]) and isinstance(value, list):
                    _require(
                        all(not isinstance(v, (dict, list)) for v in value),
                        "Некорректная коллекция OData.",
                    )
                else:
                    raise ValueError("Публикация вернула неожиданный тип реквизита.")
            elif value is not None and columns is not None:
                raise ValueError("Публикация вернула некорректную табличную часть.")
            result[name] = value
        return result


def _number(kind, value):
    _require(
        type(value) in (str, int, float, Decimal),
        "Передайте число или десятичную строку.",
    )
    literal = str(value)
    _require(
        len(literal) <= 128 and NUMBER.fullmatch(literal),
        "Требуется конечное десятичное число.",
    )
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
        suffix = {
            "Edm.Int64": "L",
            "Edm.Decimal": "M",
            "Edm.Double": "D",
            "Edm.Single": "F",
        }.get(kind, "")
        return _number(kind, value) + suffix
    if kind in ("Edm.DateTime", "Edm.DateTimeOffset"):
        offset = kind == "Edm.DateTimeOffset"
        pattern = DATE + (r"(?:Z|[+-][0-9]{2}:[0-9]{2})" if offset else "")
        _require(
            isinstance(value, str) and re.fullmatch(pattern, value),
            "Требуется дата-время ISO 8601.",
        )
        return ("datetimeoffset'" if offset else "datetime'") + value + "'"
    _require(
        kind == "Edm.String" and isinstance(value, str) and len(value) <= 2048,
        "Недопустимое условие отбора.",
    )
    return "'" + value.replace("'", "''") + "'"


def _fields(model, entity, data, *, sections=False, odata_annotations=False, depth=0):
    _require(
        depth <= 12 and isinstance(data, dict) and 1 <= len(data) <= 512,
        "Передайте от 1 до 512 реквизитов.",
    )
    encoded = {}
    for name, value in data.items():
        field = _field(entity, name)
        _require(
            depth != 0 or name not in RESERVED,
            "Изменение системного поля запрещено: " + name,
        )
        kind = field["type"]
        table = COLLECTION.fullmatch(kind)
        if table:
            _require(
                sections,
                "Для полной замены табличной части требуется replaceTableSections=true.",
            )
            row = model.resolve(table.group(1))
            _require(
                row and row["kind"] == "ComplexType",
                "Неподдерживаемый тип табличной части.",
            )
            if odata_annotations:
                encoded[name + "@odata.type"] = kind
            lines = []
            line_number_type = row["fields"].get("LineNumber", {}).get("type")
            for index, line in enumerate(_array(value, 5000, name), 1):
                encoded_line = _fields(
                    model,
                    row,
                    line,
                    odata_annotations=odata_annotations,
                    depth=depth + 1,
                )
                if line_number_type in INT_TYPES and (odata_annotations or "LineNumber" in line):
                    if "LineNumber" in line:
                        _require(
                            str(encoded_line["LineNumber"]) == str(index),
                            f"Поле LineNumber должно совпадать с позицией строки ({index}).",
                        )
                    # 1C OData requires one-based Edm.Int64 strings when creating embedded rows.
                    # The extension accepts a JSON integer for an explicitly supplied position.
                    encoded_line["LineNumber"] = (
                        str(index) if odata_annotations and line_number_type == "Edm.Int64" else index
                    )
                lines.append(encoded_line)
            value = lines
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
            value = _fields(model, item, value, odata_annotations=odata_annotations, depth=depth + 1)
        else:
            _require(isinstance(value, str), "Для поля требуется строка: " + name)
            limit = field.get("maxLength", "")
            if kind == "Edm.String" and re.fullmatch(r"[0-9]+", limit):
                size = len(value.encode("utf-16-le")) // 2
                _require(
                    size <= int(limit),
                    f"Поле {name} допускает {limit} единиц UTF-16, передано {size}.",
                )
        encoded[name] = value
    return encoded


class PostedDocumentGuardUnavailable(ValueError):
    """The extension cannot enforce the document policy. No change has been sent."""

    def __init__(self):
        super().__init__(
            "Для изменений документов обновите расширение OneCMCP до версии с защитой "
            "проведённых документов (1.2.1 или новее)."
        )


class ODataClient:
    def __init__(self, profile, transport, extension_transport=None):
        self.profile = profile
        self.transport = transport
        self.extension = extension_transport
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
        _require(
            can_read_collection(self.profile, collection),
            "Набор недоступен для чтения в этом профиле.",
        )
        model = await self._schema()
        resource = model.sets.get(collection)
        _require(resource, "Набор отсутствует в опубликованном интерфейсе OData.")
        _require(
            not ref or resource["refObject"],
            "Операция требует справочник или документ с ключом Ref_Key.",
        )
        return model, resource, model.resolve(resource["entityType"])

    async def collections(self, *, search="", top=25, skip=0):
        _page(top, skip)
        _require(
            isinstance(search, str) and len(search) <= 256,
            "Недопустимая строка поиска.",
        )
        model = await self._schema()
        needle = search.casefold()
        names = sorted(
            (
                name
                for name in model.sets
                if can_read_collection(self.profile, name) and needle in name.casefold()
            ),
            key=str.casefold,
        )
        entries = []
        for name in names[skip : skip + top]:
            resource = model.sets[name]
            view = _ReadView(self.profile, name, model, model.resolve(resource["entityType"]))
            entries.append(view.summary(resource))
        return {
            "collections": entries,
            "total": len(names),
            "skip": skip,
            "top": top,
        }

    async def describe(self, *, collection):
        model, resource, entity = await self._lookup(collection)
        view = _ReadView(self.profile, collection, model, entity)
        fields = []
        for field in entity["fields"].values():
            if field["name"] not in view.allowed:
                continue
            field = field.copy()
            table = COLLECTION.fullmatch(field["type"])
            if table and (row := model.resolve(table.group(1))):
                columns = view.allowed[field["name"]]
                field["rowFields"] = [f for n, f in row["fields"].items() if columns is None or n in columns]
            fields.append(field)
        return deepcopy({**view.summary(resource), "fields": fields})

    async def list(self, *, collection, top=25, skip=0, select=None, filter=None, orderBy=None):
        _page(top, skip)
        model, _, entity = await self._lookup(collection)
        view = _ReadView(self.profile, collection, model, entity)
        query = {"$format": "json", "$top": top, "$skip": skip, "allowedOnly": "true"}
        selected, fields = view.projection(select)
        if fields:
            query["$select"] = fields
        conditions = []
        for item in _array([] if filter is None else filter, 16, "filter"):
            _require(isinstance(item, dict), "Недопустимое условие отбора.")
            prop = view.comparable(item.get("field"))
            op = item.get("op")
            _require(
                op in ("eq", "ne", "gt", "ge", "lt", "le") and "value" in item,
                "Недопустимое условие отбора.",
            )
            conditions.append(f"{prop['name']} {op} " + _literal(prop["type"], item["value"]))
        if conditions:
            query["$filter"] = " and ".join(conditions)
        order = {}
        for item in _array([] if orderBy is None else orderBy, 16, "orderBy"):
            _require(isinstance(item, dict), "Недопустимая сортировка.")
            field = view.comparable(item.get("field"))
            direction = item.get("direction", "asc")
            _require(direction in ("asc", "desc"), "Укажите asc или desc.")
            order[field["name"]] = direction
        for key in entity["keys"]:
            if key in view.allowed:
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
            "data": [view.trim(row, selected) for row in rows],
            "top": top,
            "skip": skip,
            "nextSkip": skip + len(rows) if rows and more else None,
        }

    async def get(self, *, collection, id, select=None):
        _guid(id)
        model, _, entity = await self._lookup(collection, ref=True)
        view = _ReadView(self.profile, collection, model, entity)
        view.visible("Ref_Key")
        query = {"$format": "json"}
        selected, fields = view.projection(select, version=True)
        if fields:
            query["$select"] = fields
        response = await self.transport.request(f"{collection}(guid'{id}')", query=query)
        data = response["data"]
        row = data.get("d", data) if isinstance(data, dict) else None
        _require(isinstance(row, dict), "Некорректный ответ объекта OData.")
        projected = view.trim(row, selected)
        return {"data": projected, "etag": projected.get("DataVersion")}

    async def _prepare(
        self,
        method,
        *,
        collection,
        id=None,
        etag=None,
        data=None,
        confirmId=None,
        replaceTableSections=False,
    ):
        allowed = self.profile.get("writeCollections", [])
        _require(
            isinstance(allowed, list) and (collection in allowed or "*" in allowed),
            "Запись для набора отключена.",
        )
        _require(method in WRITE_METHODS, "Неизвестная операция.")
        creating = method == "create"
        if not creating:
            _require(_guid(id) != NULL_GUID, "Пустая ссылка недопустима для записи.")
            _version(etag)
        if method == "remove":
            _require(
                self.profile.get("physicalDelete") is True,
                "Физическое удаление отключено.",
            )
            _require(
                isinstance(confirmId, str) and confirmId.lower() == id.lower(),
                "Подтвердите точный UUID удаления.",
            )
        model, _, entity = await self._lookup(collection, ref=True)
        view = _ReadView(self.profile, collection, model, entity)
        view.visible("Ref_Key")
        if not creating:
            view.visible("DataVersion")
        body = None
        if method == "markDeletion":
            _require(
                view.visible("DeletionMark")["type"] == "Edm.Boolean",
                "Поле пометки удаления недоступно.",
            )
            body = {"DeletionMark": True}
        if creating or method == "update":
            _require(isinstance(data, dict), "Передайте реквизиты объекта.")
            for name, value in data.items():
                view.visible(name)
                columns = view.allowed[name]
                if columns is not None:
                    _require(
                        creating,
                        "Запрещена замена табличной части со скрытыми колонками.",
                    )
                    _require(
                        isinstance(value, list)
                        and all(isinstance(line, dict) and set(line) <= columns.keys() for line in value),
                        "Колонка табличной части недоступна.",
                    )
            body = _fields(
                model,
                entity,
                data,
                sections=creating or replaceTableSections is True,
                odata_annotations=creating,
            )
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

    async def _check_atomic_service(self, collection):
        _require(
            self.extension is not None,
            "Для изменения существующего объекта требуется расширение с атомарной записью.",
        )
        caps = (await self.extension.request("capabilities"))["data"]
        _require(
            isinstance(caps, dict)
            and type(caps.get("protocolVersion")) is int
            and caps.get("protocolVersion") == 1
            and caps.get("dataTransport") == "odata-v3"
            and caps.get("atomicWrites") is True,
            "Расширение не поддерживает атомарную запись. Проверьте публикацию и версию расширения.",
        )
        if collection.startswith("Document_") and caps.get("protectsPostedDocuments") is not True:
            raise PostedDocumentGuardUnavailable()

    async def validate_write(self, method, **args):
        _, preview = await self._prepare(method, **args)
        if method != "create":
            await self._check_atomic_service(args["collection"])
        return preview

    async def _mutate(self, operation, **args):
        body, _ = await self._prepare(operation, **args)
        name = args["collection"]
        if operation == "create":
            response = await self.transport.request(name, method="POST", query={"$format": "json"}, body=body)
            data = response.get("data")
            if isinstance(data, dict):
                data = data.get("d", data)
            key = data.get("Ref_Key") if isinstance(data, dict) else None
            etag = data.get("DataVersion") if isinstance(data, dict) else None
            if not isinstance(key, str) or not UUID.fullmatch(key) or key == NULL_GUID:
                key = None
            rules = self.profile.get("readFields", {}).get(name, ["*"])
            if "*" not in rules and "DataVersion" not in rules:
                etag = None
            if etag is not None:
                try:
                    _version(etag)
                except ValueError:
                    etag = None
            return {"status": response["status"], "id": key, "etag": etag}
        await self._check_atomic_service(name)
        wire = {
            "operation": {
                "update": "update",
                "markDeletion": "mark",
                "remove": "delete",
            }[operation],
            "collection": name,
            "id": args["id"],
            "version": args["etag"],
        }
        if operation == "update":
            wire["data"] = body
            if args.get("replaceTableSections") is True:
                wire["replaceTableSections"] = True
        response = await self.extension.request("change", method="POST", body=wire)
        data = response.get("data")
        try:
            _require(
                response["status"] == 200
                and isinstance(data, dict)
                and _guid(data.get("id")).lower() == args["id"].lower(),
                "Некорректный идентификатор в ответе.",
            )
            etag = data["version"]
            if operation == "remove":
                _require(etag is None, "Некорректная версия удалённого объекта.")
            else:
                _version(etag)
        except (KeyError, ValueError):
            raise TransportError(
                "Получен неполный ответ атомарной записи. Проверьте состояние объекта перед повтором."
            ) from None
        return {"status": response["status"], "id": args["id"], "etag": etag}

    async def create(self, **args):
        return await self._mutate("create", **args)

    async def update(self, **args):
        return await self._mutate("update", **args)

    async def markDeletion(self, **args):
        return await self._mutate("markDeletion", **args)

    async def remove(self, **args):
        return await self._mutate("remove", **args)
