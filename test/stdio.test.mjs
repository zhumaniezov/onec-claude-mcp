import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { mkdtemp, rmdir, unlink, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const root = fileURLToPath(new URL('../', import.meta.url));
const id = '52c82960-1351-11ee-b543-0242ac120002';
const collection = 'Catalog_Пример';
const setPath = '/demo/odata/standard.odata/' + collection;
const edmx = '<edmx:Edmx Version="1.0" xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx"><edmx:DataServices xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata" m:DataServiceVersion="3.0"><Schema Namespace="StandardODATA" xmlns="http://schemas.microsoft.com/ado/2009/11/edm"><EntityType Name="Catalog_Пример"><Key><PropertyRef Name="Ref_Key"/></Key><Property Name="Ref_Key" Type="Edm.Guid" Nullable="false"/><Property Name="DataVersion" Type="Edm.String"/><Property Name="Description" Type="Edm.String"/><Property Name="DeletionMark" Type="Edm.Boolean"/></EntityType><EntityContainer Name="Container" m:IsDefaultEntityContainer="true"><EntitySet Name="Catalog_Пример" EntityType="StandardODATA.Catalog_Пример"/></EntityContainer></Schema></edmx:DataServices></edmx:Edmx>';

async function connect(t, bases) {
  const dir = await mkdtemp(path.join(root, '.stdio-test-'));
  const config = path.join(dir, 'config.json');
  await writeFile(config, JSON.stringify({ bases }));
  t.after(async () => { await unlink(config); await rmdir(dir); });
  const transport = new StdioClientTransport({ command: process.execPath,
    args: [process.env.ONEC_TEST_ENTRY || path.join(root, 'src', 'server.mjs'), '--config', config], stderr: 'pipe' });
  const client = new Client({ name: 'acceptance-client', version: '1.0.0' });
  t.after(() => client.close());
  await client.connect(transport);
  return client;
}

test('read-only mode exposes only reads; listing profiles needs no upstream connection', async t => {
  const mcp = await connect(t, { demo: { publicationUrl: 'https://example.invalid/base' } });
  assert.ok((await mcp.listTools()).tools.every(tool => tool.annotations.readOnlyHint));
  const res = await mcp.callTool({ name: 'onec_bases', arguments: {} });
  assert.equal(JSON.parse(res.content[0].text).bases[0].name, 'demo');
});

test('Claude-compatible SDK stdio -> HTTP fixture: discovery, create, update, mark and physical delete', async t => {
  let row = null;
  const writes = [];
  const http = createServer(async (req, res) => {
    const pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
    const send = (status, data) => {
      res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
      res.end(data === undefined ? undefined : JSON.stringify(data));
    };
    if (pathname.endsWith('/capabilities')) return send(200, { protocolVersion: 1, extensionVersion: '1.0.0', dataTransport: 'odata-v3' });
    if (pathname.endsWith('/metadata')) return send(200, { objects: [{ kind: 'Catalog', name: 'Пример', synonym: 'Тестовый пример' }] });
    if (pathname.endsWith('/$metadata')) { res.setHeader('Content-Type', 'application/xml'); return res.end(edmx); }
    if (pathname === setPath && req.method === 'GET') return send(200, { value: row ? [row] : [] });
    const object = pathname === setPath + "(guid'" + id + "')";
    if (object && req.method === 'GET') return send(row ? 200 : 404, row);
    let body = '';
    for await (const part of req) body += part;
    if (pathname === setPath && req.method === 'POST') {
      writes.push('create');
      row = { ...JSON.parse(body), Ref_Key: id, DataVersion: 'AAAAAAAB', DeletionMark: false };
      return send(201, row);
    }
    if (object && row && req.headers['if-match'] !== row.DataVersion) return send(412, {});
    if (object && req.method === 'PATCH') {
      writes.push('patch');
      Object.assign(row, JSON.parse(body), { DataVersion: writes.length === 2 ? 'AAAAAAAC' : 'AAAAAAAD' });
      return send(204);
    }
    if (object && req.method === 'DELETE') { writes.push('delete'); row = null; return send(204); }
    send(404, {});
  });
  http.listen(0, '127.0.0.1');
  await once(http, 'listening');
  t.after(() => { http.closeAllConnections(); http.close(); });
  const mcp = await connect(t, { demo: { publicationUrl: 'http://127.0.0.1:' + http.address().port + '/demo',
    allowLoopbackHttp: true, writeCollections: [collection], physicalDelete: true } });
  async function call(name, args = {}) {
    const response = await mcp.callTool({ name, arguments: { base: 'demo', ...args } });
    assert.ok(!response.isError, name + ': ' + response.content[0].text);
    return JSON.parse(response.content[0].text);
  }
  const status = await call('onec_status');
  assert.equal(status.odata.ok, true, JSON.stringify(status));
  assert.equal(status.extension.ok, true);
  assert.equal((await call('onec_metadata', { search: 'тестов' })).objects[0].name, 'Пример');
  assert.equal((await call('onec_describe', { collection })).keys[0], 'Ref_Key');
  assert.deepEqual((await call('onec_list', { collection })).data, []);
  async function change(operation, args) {
    const plan = await call('onec_prepare_change', { collection, operation, ...args });
    return call('onec_apply_change', { ticket: plan.ticket });
  }
  const created = await change('create', { data: { Description: 'Один' } });
  assert.equal(created.result.data.Ref_Key, id);
  const current = () => call('onec_get', { collection, id });
  assert.equal((await current()).data.Description, 'Один');
  await change('update', { id, version: (await current()).etag, data: { Description: 'Два' } });
  assert.equal((await current()).data.Description, 'Два');
  await change('mark', { id, version: (await current()).etag });
  assert.equal((await current()).data.DeletionMark, true);
  await change('delete', { id, version: (await current()).etag });
  assert.equal(row, null);
  assert.deepEqual(writes, ['create', 'patch', 'patch', 'delete']);
});
