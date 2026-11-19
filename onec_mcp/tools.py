"""Public MCP tool definitions, shared by discovery and SDK input validation."""

from mcp.types import Tool, ToolAnnotations


def obj(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def enum(*values):
    return {"type": "string", "enum": list(values)}


def array(items, maximum, minimum=0):
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


TEXT = {"type": "string", "minLength": 1}
GUID = {"type": "string", "pattern": "^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$"}
BASE = {"base": {**TEXT, "description": "Имя подключения из onec_bases."}}
SET = {**BASE, "collection": {**TEXT, "description": "Имя EntitySet из onec_collections."}}
PAGE = {
    "top": {"type": "integer", "minimum": 1, "maximum": 100},
    "skip": {"type": "integer", "minimum": 0, "maximum": 2147483647},
}
SEARCH = {"search": {"type": "string", "maxLength": 200}}
SELECT = {"select": array(TEXT, 100, 1)}
FILTER = obj(
    {
        "field": TEXT,
        "op": enum("eq", "ne", "lt", "le", "gt", "ge"),
        "value": {"type": ["string", "number", "boolean", "null"]},
    },
    ("field", "op", "value"),
)
ORDER = obj({"field": TEXT, "direction": enum("asc", "desc")}, ("field", "direction"))


def definitions(writable):
    def tool(name, description, properties, required=(), *, read_only=True, idempotent=True):
        return Tool(
            name=name,
            description=description,
            inputSchema=obj(properties, required),
            annotations=ToolAnnotations(
                readOnlyHint=read_only,
                destructiveHint=not read_only,
                idempotentHint=idempotent,
                openWorldHint=False,
            ),
        )

    tools = [
        tool("onec_bases", "Показать настроенные базы и разрешённые для записи коллекции.", {}),
        tool("onec_status", "Проверить OData и расширение 1С в выбранной базе.", BASE, BASE),
        tool(
            "onec_metadata",
            "Найти справочники и документы по имени или синониму через расширение.",
            {**BASE, **SEARCH, **PAGE, "kind": enum("Catalog", "Document")},
            BASE,
        ),
        tool(
            "onec_collections",
            "Найти опубликованные наборы OData, включая регистры.",
            {**BASE, **SEARCH, **PAGE},
            BASE,
        ),
        tool(
            "onec_describe",
            "Получить ключи, типы полей и ограничения длины строк.",
            {**SET, **SEARCH, **PAGE},
            SET,
        ),
        tool(
            "onec_list",
            "Прочитать разрешённые поля. Ограничьте ответ через select. Фильтры объединяются условием И.",
            {**SET, **SELECT, **PAGE, "filter": array(FILTER, 16), "orderBy": array(ORDER, 10)},
            SET,
        ),
        tool(
            "onec_get",
            "Прочитать справочник или документ по GUID. Передайте полученный etag как version при изменении.",
            {**SET, "id": GUID, **SELECT},
            (*SET, "id"),
        ),
    ]
    if writable:
        tools.extend(
            [
                tool(
                    "onec_prepare_change",
                    "Подготовить одно изменение. Покажите значения до и после и изменения строк. "
                    "mark устанавливает DeletionMark, delete физически удаляет объект.",
                    {
                        **SET,
                        "operation": enum("create", "update", "mark", "delete"),
                        "id": GUID,
                        "version": {"type": "string", "minLength": 1, "maxLength": 256},
                        "data": {
                            "type": "object",
                            "description": "Реквизиты по EDMX. Передавайте длинные Decimal и Int64 строками.",
                        },
                        "replaceTableSections": {
                            "type": "boolean",
                            "description": "Полная замена передаваемых табличных частей при update.",
                        },
                    },
                    (*SET, "operation"),
                    idempotent=False,
                ),
                tool(
                    "onec_apply_change",
                    "После согласия пользователя применить показанный план по одноразовому билету. "
                    "Сохраните operationId для сверки в журнале. Проверьте verification.differences.",
                    {**BASE, "ticket": GUID},
                    ("base", "ticket"),
                    read_only=False,
                    idempotent=False,
                ),
            ]
        )
    return tools
