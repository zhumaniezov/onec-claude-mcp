import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from onec_mcp import __version__
from onec_mcp.audit import AuditLog


@asynccontextmanager
async def connect(bases):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "profiles.json"
        path.write_text(json.dumps({"bases": bases}), encoding="utf-8")
        exe = os.environ.get("ONEC_TEST_EXE")
        params = StdioServerParameters(
            command=exe or sys.executable,
            args=([] if exe else ["-m", "onec_mcp"]) + ["--config", str(path)],
        )
        async with stdio_client(params) as streams, ClientSession(*streams) as client:
            init = await client.initialize()
            yield client, init


class ShutdownTests(unittest.TestCase):
    def test_offline_audit_lookup_uses_utf8_and_does_not_create_missing_journal(self):
        exe = os.environ.get("ONEC_TEST_EXE")
        command = [exe] if exe else [sys.executable, "-m", "onec_mcp"]
        operation = "cc0d4345-8746-4ae1-9e89-f3d531a41b2d"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "журнал.sqlite3"
            args = command + ["--audit-db", str(path), "--audit-show", operation]
            missing = subprocess.run(args, capture_output=True, timeout=30)
            self.assertNotEqual(missing.returncode, 0)
            self.assertFalse(path.exists())
            audit = AuditLog(path)
            audit.start(operation, "demo", "update", "Catalog_Товары", None, "читатель")
            audit.finish(operation, "rejected", status=403)
            found = subprocess.run(args, capture_output=True, timeout=30, check=True)
            record = json.loads(found.stdout.decode("utf-8"))
            self.assertEqual(record["ib_user"], "читатель")
            self.assertEqual(record["outcome"], "rejected")

    def test_clean_eof(self):
        exe = os.environ.get("ONEC_TEST_EXE")
        command = [exe] if exe else [sys.executable, "-m", "onec_mcp"]
        example = Path(__file__).resolve().parents[1] / "examples" / "profiles.example.json"
        run = subprocess.run(command + ["--config", str(example)], input=b"", capture_output=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stderr, b"")


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_discovery_does_not_connect_to_1c(self):
        profiles = {"demo": {"publicationUrl": "https://example.invalid/demo", "label": "Проверка"}}
        async with connect(profiles) as (client, init):
            self.assertEqual(init.serverInfo.version, __version__)
            tools = (await client.list_tools()).tools
            self.assertEqual(
                {tool.name for tool in tools},
                {
                    "onec_bases",
                    "onec_status",
                    "onec_metadata",
                    "onec_collections",
                    "onec_describe",
                    "onec_list",
                    "onec_get",
                },
            )
            self.assertTrue(all(tool.annotations.readOnlyHint for tool in tools))
            response = await client.call_tool("onec_bases", {})
            self.assertEqual(
                json.loads(response.content[0].text)["bases"],
                [{"name": "demo", "label": "Проверка", "writeCollections": [], "physicalDelete": False}],
            )

    async def test_schema_and_write_permissions_are_offered_per_profile(self):
        profiles = {
            "demo": {"publicationUrl": "https://example.invalid/demo", "writeCollections": ["Catalog_Test"]}
        }
        async with connect(profiles) as (client, _):
            tools = {t.name: t for t in (await client.list_tools()).tools}
            self.assertTrue(tools["onec_apply_change"].annotations.destructiveHint)
            self.assertEqual(len(tools), 9)
            for name, args in [
                ("onec_list", {"base": "demo", "collection": "Catalog_Test", "top": 101}),
                ("onec_bases", {"unexpected": True}),
                ("onec_get", {"base": "demo", "collection": "Catalog_Test", "id": "not-a-guid"}),
                (
                    "onec_prepare_change",
                    {
                        "base": "demo",
                        "operation": "create",
                        "collection": "Catalog_Other",
                        "data": {"Description": "A"},
                    },
                ),
            ]:
                self.assertTrue((await client.call_tool(name, args)).isError, name)
