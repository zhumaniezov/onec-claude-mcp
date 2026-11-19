import unittest
from unittest.mock import AsyncMock

from test_odata import CAT, ID, REG, VERSION, XML

from onec_mcp.odata import ODataClient
from onec_mcp.profiles import can_read_collection, parse_profiles


class ReadPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.profile = {
            "readCollections": ["*"],
            "readFields": {CAT: ["Description", "Строки/Цена"]},
            "writeCollections": [CAT],
            "physicalDelete": True,
        }
        self.row = {
            "Description": "test",
            "Ref_Key": ID,
            "DataVersion": VERSION,
            "Количество": 8,
            "Строки": [{"Текст": "hidden", "Цена": "1.99"}],
            "Secret@odata.navigationLink": "hidden",
        }

        async def reply(path, **kw):
            if path == "$metadata":
                return {"status": 200, "data": XML}
            if path == CAT:
                return {"status": 200, "data": {"value": [self.row]}}
            return {"status": 200, "data": self.row}

        self.transport = AsyncMock()
        self.transport.request.side_effect = reply
        self.client = ODataClient(self.profile, self.transport)

    def test_explicit_empty_rules_close_access_and_bad_rules_fail_to_load(self):
        for fields, expected in (
            ({}, True),
            ({"readCollections": []}, False),
            ({"readFields": {}}, False),
            ({"readFields": {CAT: []}}, False),
            ({"readFields": {CAT: ["*"]}}, True),
        ):
            parsed = parse_profiles({"bases": {"a": {"publicationUrl": "https://x.test/", **fields}}})["a"]
            self.assertEqual(can_read_collection(parsed, CAT), expected)
        for fields in (
            {"readFields": None},
            {"readCollections": "*"},
            {"readFields": {CAT: "Description"}},
            {"readFields": {CAT: ["A//B"]}},
            {"readCollections": ["*", "x"]},
        ):
            with self.assertRaises(ValueError):
                parse_profiles({"bases": {"a": {"publicationUrl": "https://x.test/", **fields}}})

    async def test_default_query_and_projection_block_extra_fields_in_upstream_response(
        self,
    ):
        self.assertEqual((await self.client.collections())["total"], 1)
        catalog = (await self.client.collections())["collections"][0]
        self.assertEqual(catalog["keys"], [])
        schema = await self.client.describe(collection=CAT)
        self.assertEqual([f["name"] for f in schema["fields"]], ["Description", "Строки"])
        self.assertEqual([f["name"] for f in schema["fields"][1]["rowFields"]], ["Цена"])
        self.assertEqual(
            (await self.client.list(collection=CAT))["data"],
            [{"Description": "test", "Строки": [{"Цена": "1.99"}]}],
        )
        query = self.transport.request.await_args.kwargs["query"]
        self.assertEqual(set(query["$select"].split(",")), {"Description", "Строки/Цена"})
        self.assertNotIn("$orderby", query)
        self.assertEqual(
            (await self.client.list(collection=CAT, select=["Строки"]))["data"],
            [{"Строки": [{"Цена": "1.99"}]}],
        )
        for bad in (
            {"select": ["Количество"]},
            {"select": ["Строки/Текст"]},
            {"filter": [{"field": "Количество", "op": "eq", "value": 8}]},
            {"orderBy": [{"field": "Ref_Key", "direction": "asc"}]},
        ):
            with self.assertRaises(ValueError):
                await self.client.list(collection=CAT, **bad)
        with self.assertRaises(ValueError):
            await self.client.list(collection=REG)
        with self.assertRaises(ValueError):
            await self.client.get(collection=CAT, id=ID)

    async def test_version_and_full_table_are_not_inferred_from_access_to_other_fields(
        self,
    ):
        self.profile["readFields"][CAT].append("Ref_Key")
        row = await self.client.get(collection=CAT, id=ID, select=["Description"])
        self.assertEqual(row, {"data": {"Description": "test"}, "etag": None})
        self.assertEqual(self.transport.request.await_args.kwargs["query"]["$select"], "Description")
        self.profile["readFields"][CAT].append("DataVersion")
        with self.assertRaises(ValueError):
            await self.client.validate_write(
                "update",
                collection=CAT,
                id=ID,
                etag=VERSION,
                data={"Строки": [{"Цена": "2"}]},
                replaceTableSections=True,
            )
        with self.assertRaises(ValueError):
            await self.client.validate_write("create", collection=CAT, data={"Количество": 4})

    async def test_creation_returns_an_identifier_without_upstream_record(self):
        self.profile["readFields"][CAT].append("Ref_Key")
        self.transport.request.side_effect = lambda path, **kw: {
            "status": 200 if path == "$metadata" else 201,
            "data": XML if path == "$metadata" else self.row,
        }
        result = await self.client.create(collection=CAT, data={"Description": "test"})
        self.assertEqual(result, {"status": 201, "id": ID, "etag": None})
