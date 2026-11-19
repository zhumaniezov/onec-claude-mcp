"""MCP SDK -> stdio child -> HTTP fixture. Set ONEC_TEST_EXE to exercise a packaged binary."""

import base64
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from test_odata import CAT, ID, XML
from test_server import connect


SET_PATH = "/stand/odata/standard.odata/" + CAT
INSTANCE_PATH = f"{SET_PATH}(guid'{ID}')"
CFE = "/stand/hs/onec_mcp/v1/"


class Publication(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.row = None
        self.writes = []

    def write_record(self, method, fields):
        self.writes.append((method, fields.copy()))
        version = base64.b64encode(len(self.writes).to_bytes(8, "little")).decode(
            "ascii"
        )
        self.row.update(fields, Ref_Key=ID, DataVersion=version)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    @property
    def resource(self):
        return unquote(urlsplit(self.path).path)

    def reply(self, status, data=None):
        xml = isinstance(data, str)
        body = (
            data.encode()
            if xml
            else json.dumps(data, ensure_ascii=False).encode()
            if data is not None
            else b""
        )
        self.send_response(status)
        self.send_header(
            "Content-Type", "application/xml" if xml else "application/json"
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

    def do_GET(self):
        path = self.resource
        if path == "/stand/odata/standard.odata/$metadata":
            return self.reply(200, XML)
        if path == CFE + "capabilities":
            return self.reply(200, {"protocolVersion": 1, "dataTransport": "odata-v3"})
        if path == CFE + "metadata":
            return self.reply(
                200,
                {
                    "objects": [
                        {
                            "kind": "Catalog",
                            "name": "Товары",
                            "synonym": "Товары стенда",
                        }
                    ]
                },
            )
        if path == CFE + "field-limits/Catalog/Товары":
            return self.reply(200, {"limits": {"Description": 12}})
        if path == SET_PATH:
            return self.reply(
                200, {"value": [self.server.row] if self.server.row else []}
            )
        if path == INSTANCE_PATH and self.server.row:
            row = self.server.row
            select = parse_qs(urlsplit(self.path).query).get("$select")
            if select:
                row = {key: row[key] for key in select[0].split(",") if key in row}
            return self.reply(200, row)
        self.reply(404)

    def do_POST(self):
        if self.resource != SET_PATH or self.server.row is not None:
            return self.reply(409)
        self.server.row = {"DeletionMark": False}
        self.server.write_record("POST", self.body())
        self.reply(201, self.server.row)

    def do_PATCH(self):
        if self.resource != INSTANCE_PATH or not self.server.row:
            return self.reply(404)
        if self.headers.get("If-Match") != self.server.row["DataVersion"]:
            return self.reply(412)
        self.server.write_record("PATCH", self.body())
        self.reply(204)

    def do_DELETE(self):
        if self.resource != INSTANCE_PATH or not self.server.row:
            return self.reply(404)
        if self.headers.get("If-Match") != self.server.row["DataVersion"]:
            return self.reply(412)
        self.server.writes.append(("DELETE", {}))
        self.server.row = None
        self.reply(204)


class WireCRUDTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_catalog_lifecycle_through_stdio(self):
        fixture = Publication()
        worker = threading.Thread(
            target=fixture.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(fixture.server_close)
        self.addCleanup(fixture.shutdown)
        profile = {
            "publicationUrl": f"http://127.0.0.1:{fixture.server_port}/stand",
            "allowLoopbackHttp": True,
            "writeCollections": [CAT],
            "physicalDelete": True,
        }
        async with connect({"stand": profile}) as (client, _):

            async def call(name, **args):
                reply = await client.call_tool(name, {"base": "stand", **args})
                self.assertFalse(reply.isError, reply.content[0].text)
                return json.loads(reply.content[0].text)

            async def prepare(operation, **args):
                return await call(
                    "onec_prepare_change", collection=CAT, operation=operation, **args
                )

            async def apply(plan):
                return await call("onec_apply_change", ticket=plan["ticket"])

            async def get():
                return await call("onec_get", collection=CAT, id=ID)

            status = await call("onec_status")
            self.assertTrue(status["extension"]["ok"], status)
            self.assertEqual(status["odata"], {"ok": True, "collections": 2})
            found = await call("onec_metadata", search="стенда")
            self.assertEqual([i["name"] for i in found["objects"]], ["Товары"])
            schema = await call("onec_describe", collection=CAT)
            self.assertEqual(schema["stringLimits"]["Description"], 12)
            self.assertEqual((await call("onec_list", collection=CAT))["data"], [])

            new = await prepare("create", data={"Description": "Один"})
            self.assertIsNone(fixture.row)
            created = await apply(new)
            self.assertEqual(created["id"], ID)
            duplicate = await client.call_tool(
                "onec_apply_change", {"base": "stand", "ticket": new["ticket"]}
            )
            self.assertTrue(duplicate.isError)
            original = await get()
            self.assertEqual(original["data"]["Description"], "Один")

            update = await prepare(
                "update", id=ID, version=original["etag"], data={"Description": "Два"}
            )
            self.assertEqual(update["before"], {"Description": "Один"})
            self.assertEqual(update["after"], {"Description": "Два"})
            updated = await apply(update)
            self.assertEqual(
                updated["verification"], {"checked": True, "differences": []}
            )
            second = await get()
            self.assertEqual(second["data"]["Description"], "Два")
            self.assertNotEqual(second["etag"], original["etag"])

            marked = await apply(await prepare("mark", id=ID, version=second["etag"]))
            self.assertEqual(
                marked["verification"], {"checked": True, "differences": []}
            )
            record = await get()
            self.assertIs(record["data"]["DeletionMark"], True)
            await apply(await prepare("delete", id=ID, version=record["etag"]))
            missing = await client.call_tool(
                "onec_get", {"base": "stand", "collection": CAT, "id": ID}
            )
            self.assertTrue(missing.isError)
            self.assertIn("404", missing.content[0].text)
            self.assertEqual((await call("onec_list", collection=CAT))["data"], [])
        self.assertEqual(
            [method for method, _ in fixture.writes],
            ["POST", "PATCH", "PATCH", "DELETE"],
        )
        self.assertEqual(
            fixture.writes[1:3],
            [("PATCH", {"Description": "Два"}), ("PATCH", {"DeletionMark": True})],
        )
