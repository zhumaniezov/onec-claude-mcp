import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from onec_mcp.changes import ChangePlans

ID = "aaaaaaaa-1111-4111-8111-111111111111"
COLLECTION = "Catalog_Demo"
REQUEST = {
    "base": "demo",
    "operation": "update",
    "collection": COLLECTION,
    "id": ID,
    "version": "AAAA=",
    "data": {"Description": "Новое"},
}


class Client:
    def __init__(self):
        self.row = {"Ref_Key": ID, "Description": "Старое", "DataVersion": "AAAA=", "DeletionMark": False}
        self.writes = []
        self.reads = []

    async def validate_write(self, method, **args):
        pass

    async def describe(self, **args):
        return {"fields": [{"name": f} for f in self.row]}

    async def get(self, *, collection, id, select):
        self.reads.append(select)
        return {"data": {f: self.row[f] for f in select if f in self.row}, "etag": self.row["DataVersion"]}

    async def update(self, **args):
        self.writes.append(args)
        self.row.update(args["data"])
        return {"status": 204}

    async def create(self, **args):
        self.writes.append(args)
        return {"status": 204}

    async def remove(self, **args):
        self.writes.append(args)
        return {"status": 204}

    async def markDeletion(self, **args):
        self.writes.append(args)
        self.row["DeletionMark"] = True
        return {"status": 204}


class ChangeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = Client()
        self.profile = {"writeCollections": [COLLECTION], "physicalDelete": False}
        self.session = SimpleNamespace(profile=self.profile, client=self.client, field_limits=None)
        self.plans = ChangePlans(lambda base: self.session)

    async def prepare(self, **kwargs):
        return await self.plans.prepare(**(copy.deepcopy(REQUEST) | kwargs))

    async def apply(self, plan):
        return await self.plans.apply(base="demo", ticket=plan["ticket"])

    async def test_preview_bound_to_base_and_immutable_single_use(self):
        plan = await self.prepare()
        self.assertEqual(plan["before"], {"Description": "Старое"})
        self.assertEqual(self.client.reads, [["Description"]])
        self.assertEqual(self.client.writes, [])
        self.assertTrue(plan["expiresAt"].endswith("Z"))
        plan["after"]["Description"] = "подмена"
        with self.assertRaisesRegex(ValueError, "другой базы"):
            await self.plans.apply(base="other", ticket=plan["ticket"])
        result = await self.apply(plan)
        self.assertEqual(self.client.writes[0]["data"], {"Description": "Новое"})
        self.assertEqual(result["verification"], {"checked": True, "differences": []})
        self.assertEqual(result["id"], ID)
        with self.assertRaisesRegex(ValueError, "уже использован"):
            await self.apply(plan)

    async def test_stale_version_on_prepare_and_apply_and_missing_before(self):
        with self.assertRaisesRegex(ValueError, "верси"):
            await self.prepare(version="old")
        with self.assertRaisesRegex(ValueError, "значение"):
            await self.prepare(data={"missing": True})
        plan = await self.prepare()
        self.client.row["DataVersion"] = "BBB="
        with self.assertRaisesRegex(ValueError, "изменилась"):
            await self.apply(plan)
        self.assertEqual(self.client.writes, [])

    async def test_limits_are_utf16_and_unknown_limits_are_reported(self):
        async def limits(collection):
            return {"Description": 1}

        self.session.field_limits = limits
        with self.assertRaisesRegex(ValueError, "UTF-16.*2"):
            await self.prepare(data={"Description": "😀"})

        async def old_extension(collection):
            return None

        self.session.field_limits = old_extension
        self.assertIn("fieldLimits", await self.prepare())

    async def test_omitted_fields_and_nonmatching_values_in_verification(self):
        self.client.row["Rows"] = []

        async def written(**args):
            return {"status": 200, "data": {"Rows": [{"A": 1, "Generated": 2}]}}

        self.client.update = written
        plan = await self.prepare(data={"Rows": [{"A": True}]}, replaceTableSections=True)
        self.assertEqual((await self.apply(plan))["verification"]["differences"], ["Rows"])
        plan = await self.prepare(data={"Rows": [{"A": 1}]}, replaceTableSections=True)
        self.assertEqual((await self.apply(plan))["verification"]["differences"], [])

        create = await self.plans.prepare(
            base="demo", operation="create", collection=COLLECTION, data={"A": 1}
        )
        self.assertNotIn("before", create)
        applied = await self.apply(create)
        self.assertNotIn("id", applied)
        self.assertFalse(applied["verification"]["checked"])

    async def test_mutations_require_matching_inputs_and_permission(self):
        with self.assertRaises(ValueError):
            await self.plans.prepare(
                base="demo", operation="create", collection=COLLECTION, id=None, data={"A": 1}
            )
        for args in [
            {"data": {}},
            {"data": None},
            {"id": None},
            {"operation": "delete"},
            {"operation": "mark"},
        ]:
            with self.assertRaises(ValueError):
                await self.prepare(**args)
        self.profile["writeCollections"] = []
        with self.assertRaisesRegex(ValueError, "отключена"):
            await self.prepare()

    async def test_mark_and_delete_have_distinct_effect_and_result(self):
        args = {k: v for k, v in REQUEST.items() if k not in {"operation", "data"}}
        p = await self.plans.prepare(operation="mark", **args)
        self.assertEqual(p["after"], {"DeletionMark": True})
        self.assertEqual((await self.apply(p))["verification"]["differences"], [])
        with self.assertRaisesRegex(ValueError, "удаление"):
            await self.plans.prepare(operation="delete", **args)
        self.profile["physicalDelete"] = True
        p = await self.plans.prepare(operation="delete", **args)
        self.assertNotIn("after", p)
        result = await self.apply(p)
        self.assertNotIn("verification", result)
        self.assertEqual(self.client.writes[-1]["confirmId"], ID)

    async def test_competing_tickets_and_ambiguous_write_do_not_replay(self):
        a = await self.prepare()
        b = await self.prepare(id=ID.upper())
        started, finish = asyncio.Event(), asyncio.Event()

        async def write(**args):
            started.set()
            await finish.wait()
            raise OSError("lost connection")

        self.client.update = write
        first = asyncio.create_task(self.apply(a))
        try:
            await started.wait()
            with self.assertRaisesRegex(ValueError, "Идёт изменение"):
                await self.apply(b)
        finally:
            finish.set()
        with self.assertRaisesRegex(ValueError, "lost connection.*План закрыт"):
            await first
        for p in [a, b]:
            with self.assertRaisesRegex(ValueError, "уже использован"):
                await self.apply(p)

    async def test_expiration_capacity_and_preview_byte_budget(self):
        with patch("onec_mcp.changes.monotonic", return_value=100):
            plans = [await self.prepare() for _ in range(64)]
            with self.assertRaisesRegex(ValueError, "лимит"):
                await self.prepare()
        with patch("onec_mcp.changes.monotonic", return_value=400):
            with self.assertRaisesRegex(ValueError, "истёк"):
                await self.apply(plans[0])
            await self.prepare()
        with self.assertRaisesRegex(ValueError, "96 КиБ"):
            await self.prepare(data={"Description": "Ю" * 50000})
