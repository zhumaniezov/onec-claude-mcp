import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { test } from 'node:test';
import { createODataClient } from '../src/odata.mjs';

const collection = 'Catalog_Товары';
const register = 'InformationRegister_Цены';
const id = '68079aa7-5111-446a-a500-7974dae0ad74';
const etag = 'AAAAAQAABA0=';
const envelope = (body) =>
  '<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx" Version="1.0"><edmx:DataServices>' +
  '<Schema xmlns="http://schemas.microsoft.com/ado/2009/11/edm" Namespace="StandardODATA">' +
  body + '</Schema></edmx:DataServices></edmx:Edmx>';
const metadata = envelope(
  '<EntityType Name="Item"><Key><PropertyRef Name="Ref_Key"/></Key>' +
  '<Property Name="Ref_Key" Type="Edm.Guid" Nullable="false"/><Property Name="Description" Type="Edm.String" MaxLength="150"/>' +
  '<Property Name="DeletionMark" Type="Edm.Boolean"/><Property Name="DataVersion" Type="Edm.String"/>' +
  '<Property Name="Строки" Type="Collection(StandardODATA.Row)"/></EntityType>' +
  '<ComplexType Name="Row"><Property Name="Количество" Type="Edm.Decimal"/></ComplexType>' +
  '<EntityType Name="Price"><Key><PropertyRef Name="Период"/><PropertyRef Name="Товар_Key"/></Key>' +
  '<Property Name="Период" Type="Edm.DateTime"/><Property Name="Товар_Key" Type="Edm.Guid"/></EntityType>' +
  '<EntityContainer Name="Standard"><EntitySet Name="Catalog_Товары" EntityType="StandardODATA.Item"/>' +
  '<EntitySet Name="InformationRegister_Цены" EntityType="StandardODATA.Price"/></EntityContainer>');

// Only the external 1C HTTP boundary is emulated. Requests actually cross a local socket.
async function fixture(t, respond, options = {}, xml = metadata) {
  const calls = [];
  const app = createServer(async (req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    const parts = [];
    for await (const chunk of req) parts.push(chunk);
    const call = { url, method: req.method, headers: req.headers, body: Buffer.concat(parts).toString() };
    calls.push(call);
    if (url.pathname.endsWith('/$metadata')) {
      res.setHeader('Content-Type', 'application/xml');
      res.end(xml);
    } else {
      res.setHeader('Content-Type', 'application/json');
      respond(call, res);
    }
  });
  await new Promise((resolve) => app.listen(0, '127.0.0.1', resolve));
  t.after(() => { app.closeAllConnections(); return new Promise((resolve) => app.close(resolve)); });
  const baseUrl = 'http://127.0.0.1:' + app.address().port + '/demo/odata/standard.odata';
  return { client: createODataClient({ baseUrl, allowInsecureHttp: true, ...options }), calls };
}

test('uses EDMX keys to distinguish reference objects from a composite register', async (t) => {
  const { client, calls } = await fixture(t, () => assert.fail('No data fetch expected'));
  const [catalogs, info] = await Promise.all([client.collections({search: 'товАР'}), client.describe({collection: register})]);
  assert.deepEqual(catalogs.collections.map((item) => item.name), [collection]);
  assert.equal(catalogs.collections[0].refObject, true);
  assert.deepEqual(info.keys, ['Период', 'Товар_Key']);
  assert.equal(info.refObject, false);
  const item = await client.describe({collection});
  assert.equal(item.fields.find((field) => field.name === 'Description').type, 'Edm.String');
  assert.equal(calls.length, 1);
  await assert.rejects(client.get({collection: register, id}));
  await assert.rejects(client.describe({collection: '../private'}));
  assert.equal(calls.length, 1);
});

test('bounds pages and escapes Unicode identifiers, query literals and query separators', async (t) => {
  const { client, calls } = await fixture(t, (req, res) => res.end('{"value":[{"Description":"a"}]}'));
  const page = await client.list({collection, top: 3, skip: 1, select:['Description'], orderBy:[{field:'Description',direction:'desc'}],
    filter:[{field:'Description',op:'eq',value:"O'Neil & ?$top=999"}]});
  assert.deepEqual(page.data, [{Description:'a'}]);
  assert.equal(page.nextSkip, null);
  const u = calls[1].url;
  assert.equal(decodeURIComponent(u.pathname), '/demo/odata/standard.odata/Catalog_Товары');
  assert.equal(u.searchParams.get('$filter'), "Description eq 'O''Neil & ?$top=999'");
  assert.equal(u.searchParams.get('$select'), 'Description');
  assert.match(u.searchParams.get('$orderby'), /^Description desc/);
  assert.equal(u.searchParams.get('$top'), '3');
  await assert.rejects(client.list({collection, top:200}));
  await assert.rejects(client.list({collection, filter:[{field:'Unknown',op:'eq',value:'x'}]}));
  await assert.rejects(client.list({collection, filter:[{field:'Description',op:'or 1 eq',value:'x'}]}));
  assert.equal(calls.length, 2);
});

test('get returns the original 1C DataVersion and preserves exact-key routing', async (t) => {
  const {client,calls} = await fixture(t, (req,res) => res.end(JSON.stringify({Ref_Key:id,DataVersion:etag})));
  const row = await client.get({collection,id,select:['Ref_Key']});
  assert.equal(row.etag, etag);
  assert.equal(row.data.Ref_Key, id);
  assert.match(calls[1].url.searchParams.get('$select'), /DataVersion/);
  assert.equal(decodeURIComponent(calls[1].url.pathname), "/demo/odata/standard.odata/Catalog_Товары(guid'" + id + "')");
});

test('creates single objects; versioned writes preserve fields and carry no load-mode override', async (t) => {
  const {client,calls} = await fixture(t, ({method},res) => {
    if (method === 'POST') {
      res.writeHead(201); res.end(JSON.stringify({Ref_Key:id, DataVersion:etag}));
    } else { res.writeHead(204); res.end(); }
  }, {allowWrites:true,allowPhysicalDelete:true,username:'local',password:'demo'});
  const created = await client.create({collection,data:{Description:'New', Строки:[{Количество:'1.20'}]}});
  assert.equal(created.data.Ref_Key, id);
  await client.update({collection,id,etag,data:{Description:'Updated'}});
  await client.markDeletion({collection,id,etag});
  await client.remove({collection,id,etag,confirmId:id});
  assert.deepEqual(calls.map((c)=>c.method), ['GET','POST','PATCH','PATCH','DELETE']);
  assert.deepEqual(JSON.parse(calls[2].body), {Description:'Updated'});
  assert.deepEqual(JSON.parse(calls[3].body), {DeletionMark:true});
  for (const c of calls.slice(2)) {
    assert.equal(c.headers['if-match'], etag);
    assert.equal(c.headers['1c_odata-dataloadmode'],undefined);
    assert.equal(c.headers.authorization, 'Basic bG9jYWw6ZGVtbw==');
  }
});

test('write gates and reserved fields stop requests before modification', async (t) => {
  const noData = () => assert.fail('Unexpected data request');
  const {client:disabled,calls:untouched} = await fixture(t, noData);
  await assert.rejects(disabled.create({collection,data:{Description:'x'}}));
  assert.equal(untouched.length, 0);
  const {client,calls} = await fixture(t, noData, {allowWrites:true});
  for (const data of [{DataVersion:'wrong'}, {Unknown:'x'}, {DeletionMark:true}, {Ref_Key:id}, {Строки:[]}, {Description:{a:1}}]) {
    await assert.rejects(client.update({collection,id,etag,data}));
  }
  await assert.rejects(client.update({collection,id,etag:'*',data:{Description:'x'}}));
  await assert.rejects(client.remove({collection,id,etag,confirmId:id}));
  await assert.rejects(client.create({collection:register,data:{Период:'2026-01-01'}}));
  assert.ok(calls.every((c) => c.url.pathname.endsWith('/$metadata')));
  const {client:dangerous} = await fixture(t, noData, {allowWrites:true,allowPhysicalDelete:true});
  await assert.rejects(dangerous.remove({collection,id,etag,confirmId:'other'}));
});

test('surfaces a 412 without retrying or refreshing the version', async (t) => {
  const {client,calls} = await fixture(t, (req,res) => { res.writeHead(412); res.end('{}'); }, {allowWrites:true});
  await assert.rejects(client.update({collection,id,etag,data:{Description:'X'}}), /412/);
  assert.deepEqual(calls.map((c) => c.method), ['GET','PATCH']);
});

test('rejects redirected endpoints and configured credentials in an address', async (t) => {
  for (const baseUrl of ['https://alice:pwd@example.test/base', 'http://example.test/base', 'file:///tmp/a']) {
    assert.throws(() => createODataClient({baseUrl}));
  }
  const {client,calls} = await fixture(t, (req,res) => {res.writeHead(302,{Location:'/wrong'});res.end();});
  await assert.rejects(client.list({collection}), /30[12]|redirect/i);
  assert.equal(calls.length, 2);
});

test('rejects oversized and malformed replies without returning partial data', async (t) => {
  const {client} = await fixture(t, (req,res) => res.end('x'.repeat(6000)), {maxResponseBytes:4096});
  await assert.rejects(client.list({collection}), /limit|large|размер/i);
  const {client:invalid} = await fixture(t, (req,res) => res.end('{"value":{}}'));
  await assert.rejects(invalid.list({collection}));
  const {client:invalidMetadata} = await fixture(t, () => {}, {}, '<html>Login</html>');
  await assert.rejects(invalidMetadata.collections());
});

test('keeps precise decimal values during read, preview and explicit table replacement', async (t) => {
  const {client,calls} = await fixture(t, (req,res) => {
    if (req.method === 'GET') {
      res.end('{"DataVersion":"AAAAAQAABA0=","Строки":[{"Количество":12345678901234.56789},{"Количество":9007199254740993},{"Количество":0.1}]}');
    } else { res.writeHead(204); res.end(); }
  }, {allowWrites:true});
  const row = await client.get({collection,id});
  const data = {Строки:row.data.Строки};
  assert.deepEqual(data.Строки.map((line) => line.Количество), ['12345678901234.56789','9007199254740993',0.1]);
  const args = {collection,id,etag,data,replaceTableSections:true};
  await client.validateWrite('update',args);
  assert.deepEqual(calls.map((call) => call.method), ['GET','GET']);
  await client.update(args);
  const sent = JSON.parse(calls[2].body);
  assert.equal(sent['Строки@odata.type'],'Collection(StandardODATA.Row)');
  assert.ok(calls[2].body.includes('12345678901234.56789'));
  assert.ok(calls[2].body.includes('9007199254740993'));
});

test('never retries a write after the server loses the HTTP response', async (t) => {
  const {client,calls} = await fixture(t, (req,res) => res.destroy(), {allowWrites:true});
  await assert.rejects(client.create({collection,data:{Description:'new'}}), /результат записи.*неизвестен/i);
  assert.deepEqual(calls.map((call) => call.method), ['GET','POST']);
});

test('wraps composite-register predicates using EDMX-guided literals', async (t) => {
  const {client,calls} = await fixture(t, (req,res) => res.end('{"value":[]}'));
  await client.list({collection:register,filter:[{field:'Товар_Key',op:'eq',value:id},
    {field:'Период',op:'ge',value:'2025-06-01T00:00:00'}]});
  assert.equal(calls[1].url.searchParams.get('$filter'), "Товар_Key eq guid'" + id + "' and Период ge datetime'2025-06-01T00:00:00'");
  assert.equal(calls[1].url.searchParams.get('allowedOnly'), 'true');
});

test('enforces an advertised string length in UTF-16 units, including tabular section rows', async (t) => {
  const xml = metadata.replace('MaxLength="150"', 'MaxLength="25"')
    .replace('<ComplexType Name="Row">','<ComplexType Name="Row"><Property Name="Текст" Type="Edm.String" MaxLength="3"/>');
  const {client,calls} = await fixture(t, () => assert.fail('Validation must not write data'), {allowWrites:true}, xml);
  const validate = (data) => client.validateWrite('create', {collection,data});
  await validate({Description:'Ж'.repeat(23) + '🙂'});
  await validate({Строки:[{Текст:'Я🙂'}]});
  await assert.rejects(validate({Description:'А'.repeat(26)}), /Description/);
  await assert.rejects(validate({Description:'Б'.repeat(24) + '🙂'}), /Description/);
  await assert.rejects(validate({Строки:[{Текст:'🙂🙂'}]}), /Текст/);
  assert.deepEqual(calls.map((c) => c.method), ['GET']);
});
