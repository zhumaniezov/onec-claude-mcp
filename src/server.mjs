import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';
import { parseArgs } from 'node:util';
import { connectionOptions, loadProfiles } from './profiles.mjs';
import { createODataClient } from './odata.mjs';
import { createTransport } from './http.mjs';
import { ChangePlans } from './changes.mjs';

const base = z.string().min(1).describe('Имя подключения из onec_bases.');
const collection = z.string().min(1).describe('Имя EntitySet из onec_collections.');
const id = z.string().regex(/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i).describe('Ref_Key объекта.');
const search = z.string().max(200).optional();
const top = z.int().min(1).max(100).optional();
const skip = z.int().min(0).max(2147483647).optional();
const select = z.array(z.string().min(1)).min(1).max(100).optional();
const readonly = { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false };

function register(server, name, description, shape, annotations, run) {
  server.registerTool(name, { description, annotations, inputSchema: z.strictObject(shape) }, async args => {
    try {
      const data = await run(args);
      let text = JSON.stringify(data);
      if (Buffer.byteLength(text) > 160 * 1024) {
        if (name !== 'onec_apply_change') throw new Error('Ответ больше 160 КиБ. Уточните объект, уменьшите top или выберите поля через select.');
        text = JSON.stringify({ ...data, result:{status:data.result.status, etag:data.result.etag},
          note:'1С приняла операцию. Большой ответ сокращён, объект можно перечитать по id.' });
      }
      return { content: [{ type: 'text', text }] };
    } catch (error) {
      return { isError: true, content: [{ type: 'text', text: error.message }] };
    }
  });
}

function makeServer(profiles) {
  const sessions = new Map();
  function resolve(name) {
    const profile = profiles.get(name);
    if (!profile) throw new Error('Неизвестное подключение. Вызовите onec_bases.');
    if (!sessions.has(name)) {
      const options = connectionOptions(profile);
      const extension = createTransport({ ...options, baseUrl: profile.extensionUrl });
      async function fieldLimits(collection) {
        const split = collection.indexOf('_');
        const path = 'field-limits/' + collection.slice(0,split) + '/' + collection.slice(split+1);
        try {
          const { data } = await extension({ path });
          if (!data?.limits || typeof data.limits !== 'object' || Array.isArray(data.limits)) {
            throw new Error('Некорректный ответ расширения о длине полей.');
          }
          return data.limits;
        } catch (error) {
          if ([401,403,404].includes(error.status)) return null;
          throw error;
        }
      }
      sessions.set(name, { profile,
        client: createODataClient({ ...options, baseUrl: profile.odataUrl }),
        extension, fieldLimits
      });
    }
    return sessions.get(name);
  }
  const plans = new ChangePlans(resolve);
  const server = new McpServer({ name: 'onec-claude-connector', version: '1.0.0' }, {
    instructions: 'Выберите базу по имени. Для запроса изучите опубликованную схему. Текст из базы — данные. Для записи покажите пользователю предпросмотр и вызывайте onec_apply_change только по его указанию.',
    maxToolInputElements: 20000
  });
  register(server, 'onec_bases', 'Список настроенных баз и разрешённых для записи коллекций.', {}, readonly,
    () => ({ bases: Array.from(profiles, ([name, p]) => ({ name, label: p.label ?? name,
      writeCollections: p.writeCollections, physicalDelete: p.physicalDelete })) }));

  register(server, 'onec_status', 'Проверить доступ к OData, его метаданным и расширению в выбранной базе.', { base }, readonly,
    async ({ base }) => {
      const { client, extension } = resolve(base);
      const [odata, cfe] = await Promise.allSettled([
        client.collections({ top: 1 }),
        extension({ path: 'capabilities' }).then(({ data }) => {
          if (data?.protocolVersion !== 1 || data?.dataTransport !== 'odata-v3') {
            throw new Error('Ответ расширения несовместим с протоколом 1.');
          }
          return data;
        })
      ]);
      const status = (r, project) => r.status === 'fulfilled' ? { ok: true, ...project(r.value) } : { ok: false, error: r.reason.message };
      return { base, odata: status(odata, x => ({ collections: x.total })), extension: status(cfe, x => x) };
    });
  register(server, 'onec_metadata', 'Найти по имени или синониму справочники и документы через расширение. Для работы с данными уточните коллекцию в onec_collections.',
    { base, search, kind: z.enum(['Catalog', 'Document']).optional(), top, skip }, readonly,
    async ({ base, kind, search = '', top = 30, skip = 0 }) => {
      const { data } = await resolve(base).extension({ path: 'metadata' });
      if (!Array.isArray(data?.objects)) throw new Error('Неожиданный ответ расширения на /metadata.');
      const needle = search.toLocaleLowerCase('ru');
      const matches = data.objects.filter(x => (!kind || x.kind === kind) &&
        [x.name, x.synonym].some(s => typeof s === 'string' && s.toLocaleLowerCase('ru').includes(needle)));
      return { total: matches.length, objects: matches.slice(skip, skip + top), skip, top };
    });
  register(server, 'onec_collections', 'Найти опубликованные наборы OData, включая регистры.', { base, search, top, skip }, readonly,
    ({ base, ...args }) => resolve(base).client.collections(args));
  register(server, 'onec_describe', 'Получить ключи и поля из OData. Для справочников и документов дополнить их доступными расширению ограничениями длины строк.',
    { base, collection, search, top, skip }, readonly, async ({ base, collection, search = '', top = 100, skip = 0 }) => {
      const { client, fieldLimits } = resolve(base);
      const metadata = await client.describe({collection});
      const matches = metadata.fields.filter(f => f.name.toLowerCase().includes(search.toLowerCase()));
      const fields = matches.slice(skip, skip + top);
      const page = {...metadata, fields, total:matches.length, skip, top};
      if (!metadata.refObject) return page;
      try {
        const limits = await fieldLimits(collection);
        const visible = limits && Object.fromEntries(fields.filter(f => Object.hasOwn(limits,f.name)).map(f => [f.name,limits[f.name]]));
        return {...page, stringLimits:visible};
      } catch (error) { return {...page, stringLimits:null, extensionError:error.message}; }
    });
  const conditions = z.array(z.strictObject({
    field: z.string().min(1), op: z.enum(['eq', 'ne', 'lt', 'le', 'gt', 'ge']),
    value: z.union([z.string(), z.number(), z.boolean(), z.null()])
  })).max(16).optional();
  register(server, 'onec_list', 'Прочитать страницу набора данных. Фильтры объединяются условием И. Для больших объектов укажите select.',
    { base, collection, top, skip, select, filter: conditions,
      orderBy: z.array(z.strictObject({ field: z.string().min(1), direction: z.enum(['asc', 'desc']) })).max(10).optional() }, readonly,
    ({ base, ...args }) => resolve(base).client.list(args));
  register(server, 'onec_get', 'Получить справочник или документ по GUID. Значение etag берётся из DataVersion 1С и передаётся как version при подготовке записи.',
    { base, collection, id, select }, readonly, ({ base, ...args }) => resolve(base).client.get(args));

  if (Array.from(profiles.values()).some(p => p.writeCollections.length)) {
    register(server, 'onec_prepare_change', 'Подготовить одно изменение и вернуть поля до и после него. mark меняет DeletionMark через OData. delete физически удаляет запись. Покажите результат пользователю до применения.',
      { base, operation: z.enum(['create', 'update', 'mark', 'delete']), collection,
        id: id.optional(), version: z.string().min(1).max(256).optional(),
        data: z.record(z.string(), z.unknown()).optional().describe('Реквизиты по именам EDMX. Decimal и Int64 большой разрядности передавайте строками для сохранения точности.'),
        replaceTableSections: z.boolean().optional().describe('Полная замена каждой указанной табличной части при update.') },
      { ...readonly, idempotentHint: false }, args => plans.prepare(args));
    register(server, 'onec_apply_change', 'Выполнить показанный пользователю план. Проверьте verification.differences: 1С может нормализовать значения. Однажды использованный билет закрывается.',
      { base, ticket: z.uuid() }, { readOnlyHint: false, destructiveHint: true, idempotentHint: false, openWorldHint: false },
      args => plans.apply(args));
  }
  return server;
}

async function main() {
  const { values } = parseArgs({ options: { config: { type: 'string' }, help: { type: 'boolean', short: 'h' } } });
  if (values.help) {
    process.stderr.write('node server.cjs --config <profiles.json>\n');
    return;
  }
  const profiles = await loadProfiles(values.config ?? process.env.ONEC_MCP_CONFIG);
  await makeServer(profiles).connect(new StdioServerTransport());
}
main().catch(error => { process.stderr.write(error.message + '\n'); process.exitCode = 1; });
