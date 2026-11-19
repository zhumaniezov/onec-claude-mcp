import { randomUUID } from 'node:crypto';

const methods = new Map([['create', 'create'], ['update', 'update'], ['mark', 'markDeletion'], ['delete', 'remove']]);
const lifetimeMs = 5 * 60 * 1000;
const maxPreviewBytes = 96 * 1024;

function matchesSubmitted(actual, expected) {
  if (Array.isArray(expected)) return Array.isArray(actual) && actual.length === expected.length &&
    expected.every((row, index) => matchesSubmitted(actual[index], row));
  if (expected && typeof expected === 'object') return actual && typeof actual === 'object' &&
    Object.entries(expected).every(([key, value]) => Object.hasOwn(actual, key) && matchesSubmitted(actual[key], value));
  return actual === expected;
}

async function checkWritten(client, entry, result) {
  const expected = entry.method === 'markDeletion' ? { DeletionMark: true } : entry.args.data;
  const fields = Object.keys(expected);
  let actual = result.data;
  if (!actual || fields.some(k => !Object.hasOwn(actual, k))) {
    const id = entry.args.id ?? actual?.Ref_Key;
    if (!id) return { checked: false, message: 'Ответ 1С не содержит ключа для контрольного чтения.' };
    try { actual = (await client.get({collection:entry.args.collection, id, select:fields})).data; }
    catch (error) { return { checked: false, message: 'Запись принята. Контрольное чтение: ' + error.message }; }
  }
  const differences = fields.filter(k => !Object.hasOwn(actual,k) || !matchesSubmitted(actual[k], expected[k]));
  return { checked:true, differences };
}

export class ChangePlans {
  #entries = new Map();
  #activeObjects = new Set();
  constructor(resolve) { this.resolve = resolve; }

  async prepare({ base, operation, collection, data, id, version, replaceTableSections }) {
    const { profile, client, fieldLimits } = this.resolve(base);
    const method = methods.get(operation);
    if (!method) throw new Error('Неизвестная операция.');
    if (!profile.writeCollections.includes(collection) && !profile.writeCollections.includes('*')) {
      throw new Error('Запись для этой коллекции отключена в профиле ' + base);
    }
    if (operation === 'delete' && !profile.physicalDelete) {
      throw new Error('Физическое удаление выключено в профиле ' + base);
    }
    const isCreate = operation === 'create';
    const hasFields = isCreate || operation === 'update';
    if (isCreate ? id !== undefined || version !== undefined : !id || !version) {
      throw new Error('Для существующего объекта укажите id и version из onec_get. Для создания опустите оба.');
    }
    if (hasFields !== (data !== undefined) || (data !== undefined && !Object.keys(data).length)) {
      throw new Error('Операции create и update требуют непустой объект data.');
    }
    if (replaceTableSections !== undefined && operation !== 'update') {
      throw new Error('replaceTableSections относится только к update.');
    }
    const args = { collection };
    if (id) Object.assign(args, { id, etag: version });
    if (hasFields) args.data = data;
    if (replaceTableSections) args.replaceTableSections = true;
    if (operation === 'delete') args.confirmId = id;
    await client.validateWrite(method, args);
    const plan = { base, operation, collection };
    if (hasFields && fieldLimits && Object.values(data).some(v => typeof v === 'string')) {
      const limits = await fieldLimits(collection);
      if (limits === null) {
        plan.fieldLimits = 'Расширение не вернуло ограничения длины. После применения сверьте результат проверки полей.';
      } else {
        for (const [name, value] of Object.entries(data)) {
          const max = limits[name];
          if (typeof value === 'string' && Number.isSafeInteger(max) && max > 0 && value.length > max) {
            throw new Error('Поле ' + name + ' допускает до ' + max + ' кодовых единиц UTF-16. Получено ' + value.length + '.');
          }
        }
      }
    }
    if (id) Object.assign(plan, { id, version });
    if (!isCreate) {
      const identityFields = ['Ref_Key', 'Code', 'Description', 'Number', 'Date', 'Posted', 'DeletionMark'];
      let fields;
      if (operation === 'update') {
        fields = Object.keys(data);
      } else {
        const schema = await client.describe({ collection });
        fields = identityFields.filter(name => schema.fields.some(field => field.name === name));
      }
      const current = await client.get({ collection, id, select: fields });
      if (current.etag !== version) throw new Error('Изменилась версия объекта. Перечитайте его через onec_get.');
      if (operation === 'update') {
        if (fields.some(k => !Object.hasOwn(current.data, k))) {
          throw new Error('База не вернула текущее значение одного из изменяемых полей. Предпросмотр невозможен.');
        }
        plan.before = Object.fromEntries(fields.map(k => [k, current.data[k]]));
      } else {
        plan.before = Object.fromEntries(fields.filter(k => Object.hasOwn(current.data, k)).map(k => [k, current.data[k]]));
      }
    }
    if (hasFields) plan.after = data;
    if (operation === 'mark') {
      plan.after = { DeletionMark: true };
      plan.effect = 'Изменится поле DeletionMark. Каскадная пометка и отмена проведения командой формы здесь не выполняются.';
    }
    if (operation === 'delete') plan.effect = 'Физическое удаление объекта через OData DELETE.';
    if (replaceTableSections) plan.tableSections = 'Передаваемые табличные части будут заменены целиком.';
    if (Buffer.byteLength(JSON.stringify(plan)) > maxPreviewBytes) {
      throw new Error('Предпросмотр больше 96 КиБ. Сократите состав изменения.');
    }
    const now = Date.now();
    for (const [ticket, entry] of this.#entries) {
      if (entry.deadline <= now) this.#entries.delete(ticket);
    }
    if (this.#entries.size >= 64) throw new Error('Достигнут лимит ожидающих планов. Срок действия плана — 5 минут.');
    const ticket = randomUUID();
    this.#entries.set(ticket, { base, method, args: structuredClone(args), deadline: now + lifetimeMs });
    return { ...plan, ticket, expiresAt: new Date(now + lifetimeMs).toISOString() };
  }

  async apply({ base, ticket }) {
    const entry = this.#entries.get(ticket);
    if (!entry || entry.deadline <= Date.now()) {
      this.#entries.delete(ticket);
      throw new Error('План не найден, уже использован или истёк.');
    }
    if (entry.base !== base) throw new Error('План создан для другой базы.');
    this.#entries.delete(ticket);
    const { client } = this.resolve(base);
    const { collection, id } = entry.args;
    const key = id && JSON.stringify([base, collection, id.toLowerCase()]);
    if (key && this.#activeObjects.has(key)) throw new Error('Идёт изменение этого объекта. Перечитайте его и подготовьте новый план.');
    if (key) this.#activeObjects.add(key);
    try {
      // Some 1C publications accept stale If-Match values. Narrow the external race window
      // and block concurrent edits from this process through the same profile.
      if (id) {
        const current = await client.get({ collection, id, select:['DataVersion'] });
        if (current.etag !== entry.args.etag) throw new Error('Версия объекта изменилась после предпросмотра. Запись остановлена.');
      }
      const result = await client[entry.method](entry.args);
      const verification = entry.method === 'remove' ? undefined : await checkWritten(client, entry, result);
      return { base, collection, id:id ?? result.data?.Ref_Key, result, verification };
    } catch (error) {
      throw new Error(error.message + ' План закрыт. Перед повторной операцией проверьте состояние объекта в 1С.');
    } finally {
      if (key) this.#activeObjects.delete(key);
    }
  }
}
