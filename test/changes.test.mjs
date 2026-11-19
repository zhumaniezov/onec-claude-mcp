import test from 'node:test';
import assert from 'node:assert/strict';
import { ChangePlans } from '../src/changes.mjs';

const id = '11111111-1111-4111-8111-111111111111';
const collection = 'Catalog_Demo';
const request = { base: 'test', operation: 'update', collection, id, version: 'AAAA=', data: { Description: 'Новое' } };
function setup(overrides = {}) {
  const writes = [];
  const profile = { writeCollections: [collection], physicalDelete: false, ...overrides };
  const client = {
    async validateWrite() {},
    async get() { return { data: { Description: 'Старое', Ref_Key: id }, etag: 'AAAA=' }; },
    async update(args) { writes.push(args); return { status: 204 }; }
  };
  return { plans: new ChangePlans(() => ({ profile, client })), writes, client };
}

test('prepare returns a before/after preview and a single-use ticket bound to a specific base', async () => {
  const { plans, writes } = setup();
  const plan = await plans.prepare(request);
  assert.deepEqual(plan.before, { Description: 'Старое' });
  assert.deepEqual(plan.after, { Description: 'Новое' });
  assert.equal(writes.length, 0);
  await assert.rejects(plans.apply({ base: 'another', ticket: plan.ticket }), /баз/i);
  const apply = () => plans.apply({ base: 'test', ticket: plan.ticket });
  const both = await Promise.allSettled([apply(), apply()]);
  assert.equal(both.filter(r => r.status === 'fulfilled').length, 1);
  assert.equal(writes.length, 1);
  assert.equal(writes[0].etag, 'AAAA=');
});

test('the collection allowlist, physical-delete switch, and stale versions block preparation', async () => {
  await assert.rejects(setup({ writeCollections: [] }).plans.prepare(request), /запис/i);
  await assert.rejects(setup().plans.prepare({ ...request, operation: 'delete', data: undefined }), /удален/iu);
  await assert.rejects(setup().plans.prepare({ ...request, version: 'OLD=' }), /верси/i);
});

test('an ambiguous write failure consumes the ticket', async () => {
  const { plans, client } = setup();
  client.update = async () => { throw new Error('lost connection'); };
  const { ticket } = await plans.prepare(request);
  await assert.rejects(plans.apply({ base: 'test', ticket }), /lost connection/);
  await assert.rejects(plans.apply({ base: 'test', ticket }), /план/i);
});

test('preparing an edit reads only affected fields and refuses an incomplete before image', async () => {
  const { plans, client, writes } = setup();
  client.get = async args => {
    assert.deepEqual(args.select, ['Description']);
    return { data: {}, etag: 'AAAA=' };
  };
  await assert.rejects(plans.prepare(request), /значение/i);
  assert.equal(writes.length, 0);
});

test('rechecks the 1C version when applying a ticket even if the publisher ignores If-Match', async () => {
  const { plans, client, writes } = setup();
  const p = await plans.prepare(request);
  client.get = async () => ({ data: { Description: 'Уже изменено' }, etag: 'BBBB=' });
  await assert.rejects(plans.apply({base:'test',ticket:p.ticket}), /верси/i);
  assert.equal(writes.length, 0);
});

test('the extension supplies a string limit absent from the EDMX', async () => {
  const {client,writes} = setup();
  const plans = new ChangePlans(() => ({profile:{writeCollections:[collection]}, client, fieldLimits: async () => ({Description:3})}));
  await assert.rejects(plans.prepare(request), /Description.*3/);
  assert.equal(writes.length,0);
});

test('reports a value shortened by 1C as a successful write with a difference', async () => {
  const { plans, client } = setup();
  client.update = async () => ({status:200,data:{Description:'Нов',Ref_Key:id,DataVersion:'BBBB='}});
  const p = await plans.prepare(request);
  const result = await plans.apply({base:'test',ticket:p.ticket});
  assert.equal(result.result.status,200);
  assert.deepEqual(result.verification.differences,['Description']);
});

test('two independently prepared tickets cannot edit the same object concurrently in one profile', async () => {
  const {plans,client} = setup();
  const a = await plans.prepare(request), b = await plans.prepare(request);
  let finish;
  const gate = new Promise(resolve => { finish = resolve; });
  client.update = async () => { await gate; return {status:200,data:{Description:'Новое'}}; };
  const first = plans.apply({base:'test',ticket:a.ticket});
  try {
    await assert.rejects(plans.apply({base:'test',ticket:b.ticket}), /Идёт изменение/);
  } finally { finish(); }
  assert.equal((await first).result.status, 200);
});
