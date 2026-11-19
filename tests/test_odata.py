import asyncio
import gzip
import json
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from onec_mcp.odata import ODataClient
from onec_mcp.transport import PostedDocumentError, Transport, TransportError

CAT = "Catalog_Товары"
REG = "InformationRegister_Цены"
ID = "68079aa7-5111-446a-a500-7974dae0ad74"
VERSION = "AAAAAQAABA0="
XML = """<e:Edmx xmlns:e="http://schemas.microsoft.com/ado/2007/06/edmx" Version="1.0"><e:DataServices>
<Schema xmlns="http://schemas.microsoft.com/ado/2009/11/edm" Namespace="Stand" Alias="S">
 <EntityType Name="Goods"><Key><PropertyRef Name="Ref_Key"/></Key>
  <Property Name="Ref_Key" Type="Edm.Guid" Nullable="false"/><Property Name="DataVersion" Type="Edm.String"/>
  <Property Name="Description" Type="Edm.String" MaxLength="12"/><Property Name="DeletionMark" Type="Edm.Boolean"/>
  <Property Name="Строки" Type="Collection(S.Row)"/><Property Name="Количество" Type="Edm.Double"/>
 </EntityType>
 <ComplexType Name="Row"><Property Name="Текст" Type="Edm.String" MaxLength="3"/><Property Name="Цена" Type="Edm.Decimal"/></ComplexType>
 <EntityType Name="Prices"><Key><PropertyRef Name="Period"/><PropertyRef Name="Товар_Key"/></Key>
  <Property Name="Period" Type="Edm.DateTime"/><Property Name="Товар_Key" Type="Edm.Guid"/></EntityType>
 <EntityContainer Name="D"><EntitySet Name="Catalog_Товары" EntityType="S.Goods"/>
  <EntitySet Name="InformationRegister_Цены" EntityType="Stand.Prices"/></EntityContainer>
</Schema></e:DataServices></e:Edmx>"""


class TestOData(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.reply = (200, b'{"value":[]}', {})
        self.capabilities = (
            200,
            b'{"protocolVersion":1,"dataTransport":"odata-v3","atomicWrites":true}',
            {},
        )
        self.xml = XML
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_call(self):
                url = urlsplit(self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                fixture.calls.append(
                    (
                        self.command,
                        unquote(url.path),
                        parse_qs(url.query),
                        self.headers,
                        body,
                    )
                )
                if url.path.endswith("$metadata"):
                    status, data, headers = 200, fixture.xml.encode(), {}
                elif url.path == "/ref/hs/onec_mcp/v1/capabilities":
                    status, data, headers = fixture.capabilities
                else:
                    status, data, headers = fixture.reply
                if status is None:
                    self.close_connection = True
                    return
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_PATCH = do_POST = do_DELETE = handle_call

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(0.02), daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/ref/odata/standard.odata"
        self.http = Transport(self.url, allow_insecure_http=True, username="local", password="demo")
        self.profile = {"writeCollections": [CAT], "physicalDelete": True}
        self.extension = Transport(
            self.url.replace("/odata/standard.odata", "/hs/onec_mcp/v1"),
            allow_insecure_http=True,
        )
        self.client = ODataClient(self.profile, self.http, self.extension)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    async def test_old_extension_cannot_receive_document_changes(self):
        document = "Document_Заказ"
        self.xml = XML.replace(CAT, document)
        self.profile["writeCollections"] = [document]
        for operation, extra in (
            ("update", {"data": {"Description": "new"}}),
            ("markDeletion", {}),
            ("remove", {"confirmId": ID}),
        ):
            args = {"collection": document, "id": ID, "etag": VERSION, **extra}
            with self.assertRaisesRegex(ValueError, "1.2.1"):
                await self.client.validate_write(operation, **args)
            with self.assertRaisesRegex(ValueError, "1.2.1"):
                await getattr(self.client, operation)(**args)
        self.assertFalse(any(method == "POST" for method, *_ in self.calls))

    async def test_only_recognized_cfe_rejection_exposes_a_localized_reason(self):
        self.reply = 422, b'{"error":"posted_document","message":"secret-sentinel"}', {}
        with self.assertRaisesRegex(PostedDocumentError, "проведённым") as refused:
            await self.extension.request("change", method="POST", body={})
        self.assertNotIn("secret-sentinel", str(refused.exception))
        for payload in (b'{"error":"secret-sentinel"}', b"not json", b"x" * 2048):
            self.reply = 422, payload, {}
            with self.assertRaises(TransportError) as generic:
                await self.extension.request("change", method="POST", body={})
            self.assertNotIsInstance(generic.exception, PostedDocumentError)
            self.assertEqual(generic.exception.status, 422)
            self.assertNotIn("secret-sentinel", str(generic.exception))

    async def test_metadata_across_types_is_cached_and_finds_fields_in_row_types(self):
        catalog, register = await asyncio.gather(
            self.client.describe(collection=CAT), self.client.describe(collection=REG)
        )
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(catalog["refObject"])
        self.assertEqual(register["keys"], ["Period", "Товар_Key"])
        self.assertFalse(register["refObject"])
        table = next(field for field in catalog["fields"] if field["name"] == "Строки")
        self.assertEqual([f["name"] for f in table["rowFields"]], ["Текст", "Цена"])
        published = await self.client.collections(search="тоВАр")
        self.assertEqual([s["name"] for s in published["collections"]], [CAT])
        with self.assertRaises(ValueError):
            await self.client.get(collection=REG, id=ID)
        self.assertEqual(len(self.calls), 1)

    async def test_query_is_validated_and_bound_to_the_configured_resource(self):
        await self.client.list(
            collection=CAT,
            top=2,
            select=["Description"],
            skip=3,
            filter=[{"field": "Description", "op": "eq", "value": "О'Нил & ?$top=1000"}],
            orderBy=[{"field": "Description", "direction": "desc"}],
        )
        _, path, query, headers, _ = self.calls[-1]
        self.assertEqual(path, "/ref/odata/standard.odata/" + CAT)
        self.assertEqual(query["$filter"], ["Description eq 'О''Нил & ?$top=1000'"])
        self.assertEqual(query["$orderby"], ["Description desc,Ref_Key asc"])
        self.assertEqual(query["$top"], ["2"])
        self.assertEqual(headers["Authorization"], "Basic bG9jYWw6ZGVtbw==")
        await self.client.list(collection=REG, filter=[{"field": "Товар_Key", "op": "eq", "value": ID}])
        self.assertEqual(self.calls[-1][2]["$filter"], ["Товар_Key eq guid'" + ID + "'"])
        for invalid in (
            {"select": ["../"]},
            {"top": 101},
            {"orderBy": [{"field": "Ref_Key", "direction": "?x=1"}]},
        ):
            with self.assertRaises(ValueError):
                await self.client.list(collection=CAT, **invalid)
        self.assertEqual(len(self.calls), 3)

    async def test_read_and_write_preserve_exact_values_and_1c_version(self):
        self.reply = (
            200,
            (
                '{"DataVersion":"' + VERSION + '","Строки":[{"Цена":12345678901234.56789}],'
                '"Количество":9007199254740993}'
            ).encode(),
            {},
        )
        record = await self.client.get(collection=CAT, id=ID, select=["Строки", "Количество"])
        self.assertEqual(record["etag"], VERSION)
        self.assertEqual(record["data"]["Количество"], "9007199254740993")
        rows = record["data"]["Строки"]
        self.assertEqual(rows[0]["Цена"], "12345678901234.56789")
        self.assertIn("DataVersion", self.calls[-1][2]["$select"][0])
        self.assertTrue(self.calls[-1][1].endswith(CAT + "(guid'" + ID + "')"))
        args = {
            "collection": CAT,
            "id": ID,
            "etag": VERSION,
            "data": {"Строки": rows, "Количество": "9007199254740993"},
            "replaceTableSections": True,
        }
        await self.client.validate_write("update", **args)
        self.assertEqual([call[0] for call in self.calls], ["GET", "GET", "GET"])
        self.reply = (
            200,
            json.dumps({"id": ID, "version": VERSION, "privateField": "hidden"}).encode(),
            {},
        )
        self.assertEqual(await self.client.update(**args), {"status": 200, "id": ID, "etag": VERSION})
        method, path, _, headers, body = self.calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/ref/hs/onec_mcp/v1/change")
        self.assertIsNone(headers.get("1C_OData-DataLoadMode"))
        wire = json.loads(body)
        self.assertEqual(
            wire,
            {
                "version": VERSION,
                "operation": "update",
                "id": ID,
                "collection": CAT,
                "replaceTableSections": True,
                "data": {"Количество": 9007199254740993, "Строки": rows},
            },
        )

    async def test_write_validation_precedes_any_modification(self):
        disabled = ODataClient({"writeCollections": []}, self.http)
        with self.assertRaises(ValueError):
            await disabled.create(collection=CAT, data={"Description": "x"})
        self.assertEqual(self.calls, [])
        for data in (
            {"Ref_Key": ID},
            {"Deleted": True},
            {"DeletionMark": True},
            {"Строки": []},
            {"Description": "Ж" * 11 + "🙂"},
        ):
            with self.assertRaises(ValueError):
                await self.client.update(collection=CAT, id=ID, etag=VERSION, data=data)
        with self.assertRaises(ValueError):
            await self.client.remove(collection=CAT, id=ID, etag=VERSION, confirmId="other")
        with self.assertRaises(ValueError):
            await self.client.markDeletion(collection=CAT, id=ID, etag="*")
        with self.assertRaises(ValueError):
            await self.client.create(collection=CAT, data={"Строки": [{"Текст": "🙂🙂"}]})
        await self.client.validate_write("create", collection=CAT, data={"Description": "Ж" * 10 + "🙂"})
        self.assertTrue(all(call[0] == "GET" for call in self.calls))

    async def test_real_1c_row_numbers_on_create_and_atomic_update(self):
        self.xml = XML.replace(
            '<ComplexType Name="Row">',
            '<ComplexType Name="Row"><Property Name="LineNumber" Type="Edm.Int64"/>',
        )
        self.reply = 201, json.dumps({"Ref_Key": ID, "DataVersion": VERSION}).encode(), {}
        rows = [{"Текст": "one"}, {"Текст": "two", "LineNumber": 2}]
        await self.client.create(collection=CAT, data={"Строки": rows})
        stored = json.loads(self.calls[-1][4])["Строки"]
        self.assertEqual([row["LineNumber"] for row in stored], ["1", "2"])
        self.assertNotIn("LineNumber", rows[0])
        self.reply = 200, json.dumps({"id": ID, "version": VERSION}).encode(), {}
        await self.client.update(
            collection=CAT, id=ID, etag=VERSION, data={"Строки": stored}, replaceTableSections=True
        )
        sent = json.loads(self.calls[-1][4])["data"]["Строки"]
        self.assertEqual([row["LineNumber"] for row in sent], [1, 2])
        posts = sum(call[0] == "POST" for call in self.calls)
        with self.assertRaisesRegex(ValueError, "LineNumber"):
            await self.client.create(collection=CAT, data={"Строки": [{"LineNumber": "2"}]})
        self.assertEqual(sum(call[0] == "POST" for call in self.calls), posts)

    async def test_412_disconnect_and_redirect_are_not_retried(self):
        self.reply = 412, b"secret server message", {}
        with self.assertRaises(TransportError) as error:
            await self.client.update(collection=CAT, id=ID, etag=VERSION, data={"Description": "ok"})
        self.assertEqual(error.exception.status, 412)
        self.assertNotIn("secret", str(error.exception))
        self.reply = None, b"", {}
        with self.assertRaisesRegex(TransportError, "неизвестен"):
            await self.client.create(collection=CAT, data={"Description": "new"})
        self.reply = 302, b"", {"Location": "/never-follow"}
        with self.assertRaises(TransportError) as redirected:
            await self.client.list(collection=CAT)
        self.assertEqual(redirected.exception.status, 302)
        self.assertEqual([c[0] for c in self.calls], ["GET", "GET", "POST", "POST", "GET"])

    async def test_mark_and_deletion_use_the_version_inside_the_atomic_command(self):
        self.reply = 200, json.dumps({"id": ID, "version": VERSION}).encode(), {}
        await self.client.markDeletion(collection=CAT, id=ID, etag=VERSION)
        self.assertEqual(
            json.loads(self.calls[-1][4]),
            {"operation": "mark", "collection": CAT, "id": ID, "version": VERSION},
        )
        self.reply = 200, json.dumps({"id": ID, "version": None}).encode(), {}
        await self.client.remove(collection=CAT, id=ID, etag=VERSION, confirmId=ID)
        self.assertEqual(
            json.loads(self.calls[-1][4]),
            {"operation": "delete", "collection": CAT, "id": ID, "version": VERSION},
        )

    async def test_capability_loss_after_successful_preparation_never_writes_through_odata(
        self,
    ):
        args = {
            "collection": CAT,
            "id": ID,
            "etag": VERSION,
            "data": {"Description": "ok"},
        }
        await self.client.validate_write("update", **args)
        self.capabilities = 200, b'{"protocolVersion":1,"dataTransport":"odata-v3"}', {}
        with self.assertRaises(ValueError):
            await self.client.update(**args)
        with self.assertRaises(ValueError):
            await ODataClient(self.profile, self.http).update(**args)
        self.assertTrue(all(c[0] == "GET" for c in self.calls))

    async def test_url_limits_and_xml_errors(self):
        for url in (
            "http://example.test/",
            "file:///etc",
            "https://user:password@example.test/x",
        ):
            with self.assertRaises(ValueError):
                Transport(url, allow_insecure_http=True)
        for invalid in ("../unsafe", "https://x", "%2f", "/other"):
            with self.assertRaises(ValueError):
                await self.http.request(invalid)
        self.reply = 200, b"x" * 1024, {}
        with self.assertRaisesRegex(TransportError, "размер|Размер|лимит"):
            await self.http.request("capabilities", max_bytes=8)
        self.xml = "<!DOCTYPE foo><html/>"
        with self.assertRaises(ValueError):
            await self.client.collections()

    async def test_gzip_expansion_obeys_decoded_limit(self):
        self.reply = 200, gzip.compress(b'{"ok":true}'), {"Content-Encoding": "gzip"}
        self.assertEqual((await self.http.request("capabilities"))["data"], {"ok": True})
        self.reply = 200, gzip.compress(b"x" * 100_000), {"Content-Encoding": "gzip"}
        with self.assertRaisesRegex(TransportError, "лимит"):
            await self.http.request("capabilities", max_bytes=1024)

    async def test_deflate_and_damaged_write_response(self):
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            compressor = zlib.compressobj(wbits=wbits)
            encoded = compressor.compress(b'{"n":4}') + compressor.flush()
            self.reply = 200, encoded, {"Content-Encoding": "deflate"}
            self.assertEqual((await self.http.request("capabilities"))["data"], {"n": 4})
        self.reply = (
            201,
            gzip.compress(b'{"ok":true}')[:-6],
            {"Content-Encoding": "gzip"},
        )
        with self.assertRaisesRegex(TransportError, "неизвестен"):
            await self.client.create(collection=CAT, data={"Description": "ok"})
        self.assertEqual([c[0] for c in self.calls].count("POST"), 1)
