import asyncio
import gzip
import json
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from onec_mcp.odata import ODataClient
from onec_mcp.transport import Transport, TransportError

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
        self.xml = XML
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def handle_call(self):
                url = urlsplit(self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                fixture.calls.append(
                    (self.command, unquote(url.path), parse_qs(url.query), self.headers, body)
                )
                if url.path.endswith("$metadata"):
                    status, data, headers = 200, fixture.xml.encode(), {}
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
        self.url = "http://127.0.0.1:%d/ref/odata/standard.odata" % self.server.server_port
        self.http = Transport(self.url, allow_insecure_http=True, username="local", password="demo")
        self.profile = {"writeCollections": [CAT], "physicalDelete": True}
        self.client = ODataClient(self.profile, self.http)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

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
        self.assertEqual(len(self.calls), 2)
        self.reply = 204, b"", {}
        await self.client.update(**args)
        method, _, _, headers, body = self.calls[-1]
        self.assertEqual(method, "PATCH")
        self.assertEqual(headers["if-match"], VERSION)
        self.assertIsNone(headers.get("1C_OData-DataLoadMode"))
        self.assertEqual(json.loads(body)["Количество"], 9007199254740993)
        self.assertEqual(json.loads(body)["Строки@odata.type"], "Collection(S.Row)")
        self.assertEqual(json.loads(body)["Строки"], rows)

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
        self.assertEqual([c[0] for c in self.calls], ["GET", "PATCH", "POST", "GET"])

    async def test_delete_requires_specific_id_and_sends_conditional_header(self):
        self.reply = 204, b"", {}
        await self.client.markDeletion(collection=CAT, id=ID, etag=VERSION)
        self.assertEqual(json.loads(self.calls[-1][4]), {"DeletionMark": True})
        await self.client.remove(collection=CAT, id=ID, etag=VERSION, confirmId=ID)
        self.assertEqual((self.calls[-1][0], self.calls[-1][3]["if-match"]), ("DELETE", VERSION))

    async def test_url_limits_and_xml_errors(self):
        for url in ("http://example.test/", "file:///etc", "https://user:password@example.test/x"):
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
        self.reply = 201, gzip.compress(b'{"ok":true}')[:-6], {"Content-Encoding": "gzip"}
        with self.assertRaisesRegex(TransportError, "неизвестен"):
            await self.client.create(collection=CAT, data={"Description": "ok"})
        self.assertEqual([c[0] for c in self.calls].count("POST"), 1)
