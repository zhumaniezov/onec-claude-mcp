import asyncio
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from onec_mcp.audit import AuditError, AuditLog
from onec_mcp.changes import ChangePlans
from onec_mcp.odata import PostedDocumentGuardUnavailable
from onec_mcp.transport import ChangeRefusedError, PostedDocumentError, TransportError

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
        self.session = SimpleNamespace(
            profile=self.profile, client=self.client, field_limits=None, audit_user="integration"
        )
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.audit_path = Path(self.directory.name) / "audit.sqlite3"
        self.plans = ChangePlans(lambda base: self.session, AuditLog(self.audit_path))

    def journal(self):
        db = sqlite3.connect(self.audit_path)
        try:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM operations ORDER BY rowid")]
        finally:
            db.close()

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
        self.assertEqual(result["operationId"], plan["operationId"])
        self.assertNotEqual(plan["operationId"], plan["ticket"])
        record = self.journal()[0]
        self.assertEqual(record["operation_id"], plan["operationId"])
        self.assertEqual(
            (record["outcome"], record["object_id"], record["ib_user"]), ("committed", ID, "integration")
        )
        self.assertTrue(record["actor"] and record["started_at"] and record["finished_at"])
        self.assertFalse({"ticket", "before", "after", "data"} & set(record))
        self.assertEqual(self.plans.audit.lookup(plan["operationId"]), record)
        self.assertIsNone(self.plans.audit.lookup("' OR 1=1 --"))
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
        self.assertEqual(self.journal()[0]["outcome"], "conflict")

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
            self.client.row["Rows"] = [{"A": 1, "Generated": 2}]
            return {"status": 200}

        self.client.update = written
        plan = await self.prepare(data={"Rows": [{"A": True}]}, replaceTableSections=True)
        self.assertEqual((await self.apply(plan))["verification"]["differences"], ["Rows"])
        plan = await self.prepare(data={"Rows": [{"A": 1}]}, replaceTableSections=True)
        self.assertEqual((await self.apply(plan))["verification"]["differences"], [])

        create = await self.plans.prepare(
            base="demo", operation="create", collection=COLLECTION, data={"A": 1}
        )
        self.assertIsNone(create["before"])
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
        self.assertIsNone(p["after"])
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
        with self.assertRaisesRegex(ValueError, "неизвестен.*План закрыт"):
            await first
        self.assertEqual([r["outcome"] for r in self.journal()], ["unknown", "conflict"])
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

    async def test_row_diffs_report_positions_omitted_values_and_clear(self):
        self.client.row["Rows"] = [{"A": "x", "B": 1, "LineNumber": 1}, {"A": "y", "B": 2}, {"A": "z"}]
        plan = await self.prepare(
            data={"Rows": [{"A": "x"}, {"A": "new", "B": 2}]}, replaceTableSections=True
        )
        diff = plan["tableDiff"]["Rows"]
        self.assertEqual(diff["changedRows"], [{"row": 1, "fields": ["B"]}, {"row": 2, "fields": ["A"]}])
        self.assertEqual(diff["removedRows"], [3])
        self.assertEqual(diff["beforeCount"], 3)
        self.assertEqual(plan["before"]["Rows"], self.client.row["Rows"])
        self.assertEqual(
            (await self.prepare(data={"Rows": []}, replaceTableSections=True))["tableDiff"]["Rows"][
                "removedRows"
            ],
            [1, 2, 3],
        )
        new = await self.plans.prepare(
            base="demo", operation="create", collection=COLLECTION, data={"Rows": [{"A": True}]}
        )
        self.assertEqual(new["tableDiff"]["Rows"]["addedRows"], [1])

    async def test_durable_intent_precedes_mutation_and_filters_response_bodies(self):
        plan = await self.prepare()

        async def write(**args):
            self.assertEqual(self.journal()[0]["outcome"], "started")
            self.client.row["Description"] = args["data"]["Description"]
            return {"status": 200, "id": ID, "data": {"extra": "redacted-value"}}

        self.client.update = write
        result = await self.apply(plan)
        self.assertNotIn("redacted-value", str(result))
        stored = self.audit_path.read_bytes()
        for text in ["redacted-value", "Старое", "Новое", plan["ticket"]]:
            self.assertNotIn(text.encode(), stored)
        self.assertEqual(self.journal()[0]["finished_at"][-6:], "+00:00")

    async def test_registry_unavailable_blocks_mutation_and_finalize_error_consumes_ticket(self):
        self.assertIsNone(self.plans.audit.lookup(ID))
        self.assertFalse(self.audit_path.exists())
        plan = await self.prepare()
        self.plans.audit = AuditLog(self.audit_path.parent / "missing" / "audit.db")
        with self.assertRaisesRegex(ValueError, plan["operationId"] + ".*не отправлена"):
            await self.apply(plan)
        self.assertFalse(self.client.writes)
        self.plans.audit = AuditLog(self.audit_path)
        plan = await self.prepare()
        with (
            patch.object(self.plans.audit, "finish", side_effect=AuditError()),
            self.assertRaisesRegex(ValueError, plan["operationId"] + ".*приняла запись"),
        ):
            await self.apply(plan)
        self.assertEqual(self.journal()[0]["outcome"], "started")
        with self.assertRaisesRegex(ValueError, "уже использован"):
            await self.apply(plan)
        self.assertEqual(len(self.client.writes), 1)

    async def test_transaction_conflicts_rejections_and_network_outcomes_are_sanitized(self):
        for status, outcome in [(412, "conflict"), (403, "rejected"), (500, "unknown"), (None, "unknown")]:

            async def fail(_status=status, **args):
                raise TransportError("secret-sentinel", _status)

            self.client.update = fail
            plan = await self.prepare()
            with self.assertRaisesRegex(ValueError, plan["operationId"]) as caught:
                await self.apply(plan)
            self.assertNotIn("secret-sentinel", str(caught.exception))
            self.assertEqual(self.journal()[-1]["outcome"], outcome)
            self.assertEqual(self.journal()[-1]["http_status"], status)
        self.assertNotIn(b"secret-sentinel", self.audit_path.read_bytes())

    async def test_posted_document_rejection_is_reported_and_audited(self):
        async def reject(**args):
            raise PostedDocumentError()

        self.client.update = reject
        plan = await self.prepare()
        with self.assertRaisesRegex(ValueError, "проведённым документом") as caught:
            await self.apply(plan)
        self.assertIn(plan["operationId"], str(caught.exception))
        row = self.journal()[0]
        self.assertEqual((row["outcome"], row["http_status"]), ("rejected", 422))
        with self.assertRaisesRegex(ValueError, "уже использован"):
            await self.apply(plan)

    async def test_extension_downgrade_after_preview_is_a_local_rejection(self):
        async def refuse_unprotected_endpoint(**args):
            raise PostedDocumentGuardUnavailable()

        plan = await self.prepare()
        self.client.update = refuse_unprotected_endpoint
        with self.assertRaisesRegex(ValueError, "обновите расширение"):
            await self.apply(plan)
        self.assertEqual(self.journal()[0]["outcome"], "rejected")
        self.assertIsNone(self.journal()[0]["http_status"])

    async def test_cancelled_dispatched_write_leaves_unknown_outcome(self):
        plan = await self.prepare()

        async def cancelled(**args):
            raise asyncio.CancelledError()

        self.client.update = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.apply(plan)
        self.assertEqual(self.journal()[0]["outcome"], "unknown")


DOCUMENT = "Document_Заказ"


class DocumentClient(Client):
    """A document whose posting state, extension capability and posting outcome can be scripted."""

    def __init__(self, posted, reposts=True):
        super().__init__()
        self.row.update(Posted=posted, Number="0001")
        self.reposts = reposts

    async def validate_write(self, method, **args):
        return {"repostsPostedDocuments": self.reposts}

    async def update(self, **args):
        result = await super().update(**args)
        return {**result, "posted": self.row["Posted"], "reposted": self.row["Posted"]}

    async def post(self, **args):
        self.writes.append(("post", args))
        self.row["Posted"] = True
        return {"status": 200, "posted": True, "reposted": False}

    async def unpost(self, **args):
        self.writes.append(("unpost", args))
        self.row["Posted"] = False
        return {"status": 200, "posted": False, "reposted": False}


class PostedDocumentTests(unittest.IsolatedAsyncioTestCase):
    def use(self, posted, reposts=True, **profile):
        self.client = DocumentClient(posted, reposts)
        self.profile = {"writeCollections": [DOCUMENT], "physicalDelete": True, **profile}
        self.session = SimpleNamespace(
            profile=self.profile, client=self.client, field_limits=None, audit_user="integration"
        )
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.audit_path = Path(directory.name) / "audit.sqlite3"
        self.plans = ChangePlans(lambda base: self.session, AuditLog(self.audit_path))

    journal = ChangeTests.journal

    async def prepare(self, **kwargs):
        request = copy.deepcopy(REQUEST) | {"collection": DOCUMENT, "data": {"Description": "20"}} | kwargs
        return await self.plans.prepare(**{k: v for k, v in request.items() if v is not None})

    async def apply(self, plan):
        return await self.plans.apply(base="demo", ticket=plan["ticket"])

    async def test_update_of_posted_document_is_previewed_and_applied_as_reposting(self):
        self.use(posted=True)
        plan = await self.prepare()
        self.assertIn("перепроведением", plan["posting"])
        self.assertIn("время документа равным текущему", plan["posting"])
        self.assertEqual(plan["before"], {"Description": "Старое"})
        self.assertIn("Posted", self.client.reads[-1])
        result = await self.apply(plan)
        self.assertEqual(result["result"]["posted"], True)
        self.assertEqual(result["result"]["reposted"], True)
        self.assertEqual(result["verification"], {"checked": True, "differences": []})

    async def test_unposted_document_is_written_without_posting(self):
        self.use(posted=False)
        plan = await self.prepare()
        self.assertIn("без проведения", plan["posting"])
        self.assertNotIn("оперативно", plan["posting"])

    async def test_hidden_posting_state_gets_a_conditional_note(self):
        self.use(posted=True)
        del self.client.row["Posted"]
        plan = await self.prepare()
        self.assertIn("Если документ окажется проведён", plan["posting"])

    async def test_old_extension_stops_posted_document_change_in_preview(self):
        self.use(posted=True, reposts=False)
        for operation, extra in (("update", {}), ("mark", {"data": None})):
            with self.assertRaisesRegex(ValueError, "1.3.0"):
                await self.prepare(operation=operation, **extra)
        self.assertEqual(self.client.writes, [])

    async def test_posted_document_cannot_be_physically_deleted(self):
        self.use(posted=True)
        with self.assertRaisesRegex(ValueError, "нельзя удалить физически"):
            await self.prepare(operation="delete", data=None)

    async def test_mark_of_posted_document_announces_unposting(self):
        self.use(posted=True)
        plan = await self.prepare(operation="mark", data=None)
        self.assertIn("Проведение будет отменено", plan["posting"])
        self.assertEqual(plan["after"], {"DeletionMark": True})

    async def test_post_and_unpost_are_single_use_plans_with_verification(self):
        self.use(posted=False)
        with self.assertRaisesRegex(ValueError, "не проведён"):
            await self.prepare(operation="unpost", data=None)
        plan = await self.prepare(operation="post", data=None)
        self.assertEqual((plan["before"]["Posted"], plan["after"]), (False, {"Posted": True}))
        self.assertIn("время документа равным текущему", plan["posting"])
        result = await self.apply(plan)
        self.assertEqual(self.client.writes[-1][0], "post")
        self.assertEqual(result["result"]["posted"], True)
        self.assertEqual(result["verification"], {"checked": True, "differences": []})
        plan = await self.prepare(operation="unpost", data=None, version=self.client.row["DataVersion"])
        self.assertIn("движения документа по регистрам удалятся", plan["posting"])
        result = await self.apply(plan)
        self.assertEqual(result["verification"], {"checked": True, "differences": []})
        self.assertEqual([r["outcome"] for r in self.journal()], ["committed", "committed"])

    async def test_posting_refusal_returns_1c_messages_and_is_audited(self):
        for show, expected in ((True, True), (False, False)):
            self.use(posted=True, writeMessages=show)

            async def refuse(**args):
                raise ChangeRefusedError("posting_failed", 422, ["Недостаточно товара Шина R16: 2 из 4"])

            self.client.update = refuse
            plan = await self.prepare()
            with self.assertRaisesRegex(ValueError, "не провела документ") as caught:
                await self.apply(plan)
            self.assertEqual("Недостаточно товара" in str(caught.exception), expected)
            row = self.journal()[0]
            self.assertEqual((row["outcome"], row["http_status"]), ("rejected", 422))
            self.assertNotIn("Недостаточно", json.dumps(row, ensure_ascii=False))
