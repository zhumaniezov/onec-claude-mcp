import { SaxesParser } from 'saxes';
import { createTransport } from './http.mjs';

const uuid = /^[\da-f]{8}(?:-[\da-f]{4}){3}-[\da-f]{12}$/i;
const identifier = /^[\p{L}_][\p{L}\p{M}\p{N}_]*$/u;
const nullGuid = '00000000-0000-0000-0000-000000000000';
const reserved = new Set(['Ref_Key', 'DataVersion', 'DeletionMark', 'Posted', 'Predefined', 'PredefinedDataName']);
const integerType = /^Edm\.(?:Int16|Int32|Int64|SByte|Byte)$/;
const floatType = /^Edm\.(?:Decimal|Double|Single)$/;
const numericLiteral = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$/;
const edmSchema = /^http:\/\/schemas\.microsoft\.com\/ado\/(?:2006\/04|2007\/05|2008\/09|2009\/11)\/edm$/;
const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);

function check(condition, message) {
  if (!condition) throw new Error(message);
}

function checkName(name) {
  check(typeof name === 'string' && identifier.test(name), 'Некорректное имя в метаданных OData.');
  return name;
}

function parseSchema(xml) {
  const parser = new SaxesParser({xmlns:true});
  const stack = [];
  const types = new Map();
  const aliases = new Map();
  const sets = new Map();
  let sawEdmx = false, sawSchema = false, schema, type;
  parser.on('doctype', () => { throw new Error('DTD в описании OData запрещён.'); });
  parser.on('opentag', (tag) => {
    const parent = stack.at(-1);
    stack.push(tag.local);
    check(stack.length <= 128, 'Слишком большая глубина EDMX.');
    if (stack.length === 1) {
      sawEdmx = tag.local === 'Edmx' && tag.uri === 'http://schemas.microsoft.com/ado/2007/06/edmx';
      check(sawEdmx, 'Ожидалось описание EDMX OData v3.');
    }
    if (!edmSchema.test(tag.uri)) return;
    const attr = (key) => tag.attributes[key]?.value;
    switch (tag.local) {
      case 'Schema':
        if (parent !== 'DataServices') return;
        schema = attr('Namespace');
        check(schema, 'У схемы отсутствует пространство имен.');
        sawSchema = true;
        if (attr('Alias')) aliases.set(attr('Alias'), schema);
        return;
      case 'EntityType':
      case 'ComplexType': {
        if (parent !== 'Schema' || !schema) return;
        const qualified = schema + '.' + checkName(attr('Name'));
        check(!types.has(qualified), 'Повторяющийся тип EDMX.');
        type = { keys:[], fields: new Map(), kind:tag.local };
        types.set(qualified, type);
        return;
      }
      case 'PropertyRef':
        if (type && parent === 'Key') type.keys.push(checkName(attr('Name')));
        return;
      case 'Property': {
        if (!type || !['EntityType','ComplexType'].includes(parent)) return;
        const name = checkName(attr('Name'));
        const field = {name, type:attr('Type'), nullable:attr('Nullable') !== 'false'};
        check(field.type && !type.fields.has(name), 'Некорректное поле EDMX.');
        for (const [source, target] of [['MaxLength','maxLength'],['Precision','precision'],['Scale','scale']]) {
          if (attr(source) !== undefined) field[target] = attr(source);
        }
        type.fields.set(name, field);
        return;
      }
      case 'EntitySet': {
        if (parent !== 'EntityContainer' || !schema) return;
        const name = checkName(attr('Name'));
        check(!sets.has(name), 'В EDMX повторяется имя набора.');
        sets.set(name, {name,entityType:attr('EntityType')});
      }
    }
  });
  parser.on('closetag', (tag) => {
    stack.pop();
    if (!edmSchema.test(tag.uri)) return;
    if (tag.local === 'EntityType' || tag.local === 'ComplexType') type = undefined;
    if (tag.local === 'Schema') schema = undefined;
  });
  parser.write(xml).close();
  check(sawEdmx && sawSchema, 'В публикации отсутствует схема OData v3.');
  function resolve(qualified) {
    if (types.has(qualified)) return types.get(qualified);
    const [prefix, local] = String(qualified).split('.');
    return types.get(aliases.get(prefix) + '.' + local);
  }
  for (const set of sets.values()) {
    const entity = resolve(set.entityType);
    check(entity?.kind === 'EntityType', 'Набор с неизвестным типом в EDMX.');
    set.keys = entity.keys;
    set.refObject = /^(Catalog|Document)_/.test(set.name) && entity.keys.length === 1 &&
      entity.keys[0] === 'Ref_Key' && entity.fields.get('Ref_Key')?.type === 'Edm.Guid';
  }
  return {sets, resolve};
}

function pageBounds(top = 25, skip = 0) {
  check(Number.isInteger(top) && top >= 1 && top <= 100, 'top должен быть от 1 до 100.');
  check(Number.isSafeInteger(skip) && skip >= 0 && skip <= 2147483647, 'Недопустимое значение skip.');
  return {top, skip};
}

function checkGuid(value) {
  check(typeof value === 'string' && uuid.test(value), 'Требуется UUID ссылки 1С.');
  return value;
}

function checkVersion(value) {
  // 1C expects the original base64 DataVersion verbatim, including its padding.
  check(typeof value === 'string' && value.length <= 256 && /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{4}|[A-Za-z0-9+/]{3}=|[A-Za-z0-9+/]{2}==)$/.test(value),
    'Передайте etag из чтения объекта (исходное поле DataVersion).');
  return value;
}

function knownField(entity, name) {
  const field = entity.fields.get(name);
  check(field, 'Неизвестный реквизит. Проверьте описание набора в метаданных.');
  return field;
}

function boundedArray(value, max, name) {
  check(Array.isArray(value) && value.length <= max, name + ' должен быть массивом до ' + max + ' элементов.');
  return value;
}

function projection(entity, select, also = []) {
  if (select === undefined) return undefined;
  const fields = boundedArray(select, 128, 'select').map((name) => knownField(entity,name).name);
  check(fields.length, 'select должен содержать хотя бы один реквизит.');
  return [...new Set([...fields,...also.filter((name) => entity.fields.has(name))])].join(',');
}

function numericValue(type, value) {
  const validNumber = typeof value === 'number' && Number.isFinite(value) &&
    (!Number.isInteger(value) || Number.isSafeInteger(value));
  check((validNumber || (typeof value === 'string' && value.length <= 128)) && numericLiteral.test(String(value)),
    'Передайте точное число. Для длинных чисел используйте десятичную строку.');
  if (integerType.test(type)) check(/^-?\d+$/.test(String(value)), 'Для этого поля требуется целое число.');
  return String(value);
}

function filterLiteral(type, value) {
  if (value === null) return 'null';
  if (type === 'Edm.Guid') return "guid'" + checkGuid(value) + "'";
  if (type === 'Edm.Boolean') {
    check(typeof value === 'boolean', 'Требуется логическое значение.');
    return String(value);
  }
  if (integerType.test(type) || floatType.test(type)) {
    const suffix = {'Edm.Int64':'L', 'Edm.Decimal':'M', 'Edm.Double':'D', 'Edm.Single':'F'}[type] ?? '';
    return numericValue(type,value) + suffix;
  }
  if (type === 'Edm.DateTime' || type === 'Edm.DateTimeOffset') {
    const time = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?$/;
    const offset = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?(?:Z|[+-]\d{2}:\d{2})$/;
    check(typeof value === 'string' && (type === 'Edm.DateTime' ? time : offset).test(value), 'Нужна дата-время ISO 8601.');
    return (type === 'Edm.DateTime' ? "datetime'" : "datetimeoffset'") + value + "'";
  }
  check(type === 'Edm.String' && typeof value === 'string' && value.length <= 2048, 'Для поля не поддерживается такое условие.');
  return "'" + value.replaceAll("'", "''") + "'";
}

function filterQuery(entity, predicates) {
  return boundedArray(predicates, 16, 'filter').map(({field, op, value}) => {
    const property = knownField(entity,field);
    check(['eq','ne','gt','ge','lt','le'].includes(op), 'Неизвестный оператор фильтра.');
    return field + ' ' + op + ' ' + filterLiteral(property.type,value);
  }).join(' and ');
}

function orderQuery(entity, order) {
  const sort = new Map();
  for (const {field, direction = 'asc'} of boundedArray(order, 16, 'orderBy')) {
    knownField(entity,field);
    check(['asc','desc'].includes(direction), 'Направление сортировки должно быть asc или desc.');
    sort.set(field, direction);
  }
  for (const key of entity.keys) if (!sort.has(key)) sort.set(key, 'asc');
  return Array.from(sort, ([key,value]) => key + ' ' + value).join(',');
}

function encodeFields(data, entity, model, {sections, root = true}, depth = 0) {
  check(depth <= 12 && isObject(data), 'Данные должны быть объектом JSON.');
  const entries = Object.entries(data);
  check(entries.length > 0 && entries.length <= 512, 'Передайте от 1 до 512 реквизитов.');
  const output = Object.create(null);
  for (const [name,value] of entries) {
    const field = knownField(entity,name);
    check(!root || !reserved.has(name), 'Изменение системного поля через data запрещено: ' + name);
    const table = /^Collection\(([^)]+)\)$/.exec(field.type);
    let encoded = value;
    if (table) {
      check(sections, 'Табличная часть заменяется целиком. Укажите replaceTableSections=true после чтения всех строк.');
      const rowType = model.resolve(table[1]);
      check(rowType?.kind === 'ComplexType', 'Неподдерживаемый тип строк табличной части.');
      output[name + '@odata.type'] = field.type;
      encoded = boundedArray(value, 5000, name).map((row) => encodeFields(row,rowType,model,{sections:false,root:false},depth+1));
    } else if (value === null) {
      check(field.nullable, 'Поле не допускает null: ' + name);
    } else if (field.type === 'Edm.Guid') {
      checkGuid(value);
    } else if (integerType.test(field.type) || floatType.test(field.type)) {
      const literal = numericValue(field.type,value);
      // OData v3 JSON carries Int64 and Decimal as strings.
      encoded = ['Edm.Int64','Edm.Decimal'].includes(field.type) ? literal : JSON.rawJSON(literal);
    } else if (field.type === 'Edm.Boolean') {
      check(typeof value === 'boolean', 'Для поля требуется true/false: ' + name);
    } else if (model.resolve(field.type)?.kind === 'ComplexType') {
      encoded = encodeFields(value,model.resolve(field.type),model,{sections:false,root:false},depth+1);
    } else {
      check(typeof value === 'string', 'Для поля требуется строковое значение: ' + name);
      if (field.type === 'Edm.String' && /^\d+$/.test(field.maxLength)) {
        // 1C strings use UTF-16. Supplementary characters occupy two code units.
        check(value.length <= Number(field.maxLength), 'Поле ' + name + ' допускает ' + field.maxLength +
          ' единиц UTF-16, передано ' + value.length + '.');
      }
    }
    output[name] = encoded;
  }
  return output;
}

export function createODataClient(options) {
  const request = createTransport(options);
  let cache, expiresAt = 0;
  async function schema() {
    if (!cache || Date.now() >= expiresAt) {
      expiresAt = Date.now() + 60_000;
      cache = request({path:'$metadata',format:'text',maxBytes:options.maxMetadataBytes ?? 32 * 1024 * 1024})
        .then(({data}) => parseSchema(data)).catch((error) => {cache = undefined; throw error;});
    }
    return cache;
  }
  async function lookup(collection, requireRef = false) {
    checkName(collection);
    const model = await schema();
    const set = model.sets.get(collection);
    check(set, 'Набор отсутствует в опубликованном интерфейсе OData.');
    check(!requireRef || set.refObject, 'Операция доступна для справочников и документов с ключом Ref_Key типа Edm.Guid.');
    return {model, set, entity:model.resolve(set.entityType)};
  }
  async function collections({search = '', top, skip} = {}) {
    const bounds = pageBounds(top,skip);
    check(typeof search === 'string' && search.length <= 256, 'Некорректная строка поиска.');
    const needle = search.toLowerCase();
    const matches = [...(await schema()).sets.values()].filter(({name}) => name.toLowerCase().includes(needle))
      .sort((a,b) => a.name.localeCompare(b.name));
    return {collections:matches.slice(bounds.skip,bounds.skip+bounds.top), total:matches.length,...bounds};
  }
  async function describe({collection}) {
    const {model,set,entity} = await lookup(collection);
    const fields = [...entity.fields.values()].map((field) => {
      const table = /^Collection\(([^)]+)\)$/.exec(field.type);
      const row = table && model.resolve(table[1]);
      return row ? {...field,rowFields:[...row.fields.values()]} : field;
    });
    return {...set,fields};
  }
  async function list({collection, top, skip, select, filter = [], orderBy = []}) {
    const bounds = pageBounds(top,skip);
    const {entity} = await lookup(collection);
    const query = {$format:'json',$top:bounds.top,$skip:bounds.skip,allowedOnly:'true'};
    const order = orderQuery(entity,orderBy), conditions = filterQuery(entity,filter), fields = projection(entity,select);
    if (order) query.$orderby = order;
    if (conditions) query.$filter = conditions;
    if (fields) query.$select = fields;
    const {data} = await request({path:collection,query});
    const rows = data?.value ?? data?.d?.results;
    check(Array.isArray(rows) && rows.length <= bounds.top && rows.every(isObject), 'Некорректный ответ списка OData.');
    // Never follow an externally supplied nextLink with this profile's credentials.
    const more = rows.length > 0 && (rows.length === bounds.top || data['odata.nextLink'] || data.d?.__next);
    return {data:rows,...bounds,nextSkip:more ? bounds.skip + rows.length : null};
  }
  async function get({collection,id,select}) {
    checkGuid(id);
    const {entity} = await lookup(collection,true);
    const query = {$format:'json'};
    const fields = projection(entity,select,['DataVersion']);
    if (fields) query.$select = fields;
    const {data} = await request({path:collection + "(guid'" + id + "')",query});
    const row = data?.d ?? data;
    check(isObject(row), 'Некорректный ответ объекта OData.');
    return {data:row,etag:row.DataVersion ?? null};
  }
  async function prepareWrite(method, {collection,id,etag,data,confirmId,replaceTableSections = false}) {
    check(options.allowWrites === true, 'Запись для этого профиля отключена.');
    check(['create','update','markDeletion','remove'].includes(method), 'Неизвестная операция.');
    if (method !== 'create') {
      checkGuid(id);
      check(id !== nullGuid, 'Пустая ссылка недопустима для изменения объекта.');
      checkVersion(etag);
    }
    if (method === 'remove') {
      check(options.allowPhysicalDelete === true, 'Физическое удаление отключено.');
      check(typeof confirmId === 'string' && id.toLowerCase() === confirmId.toLowerCase(), 'Подтвердите удаление точным UUID.');
    }
    const {model, entity} = await lookup(collection,true);
    if (method !== 'create') knownField(entity,'DataVersion');
    if (method === 'markDeletion') check(knownField(entity,'DeletionMark').type === 'Edm.Boolean', 'В наборе нет поля пометки удаления.');
    let body;
    if (method === 'markDeletion') body = {DeletionMark:true};
    if (method === 'create' || method === 'update') {
      body = encodeFields(data,entity,model,{sections:method === 'create' || replaceTableSections === true});
      if (method === 'create') body = {Ref_Key:nullGuid, ...body};
    }
    const preview = {method,collection,...(id ? {id} : {}),fields:method === 'markDeletion' ? ['DeletionMark'] : Object.keys(data ?? {})};
    return {body,preview};
  }
  async function validateWrite(method, args) {
    return (await prepareWrite(method,args)).preview;
  }
  async function mutate(operation, args) {
    const {body} = await prepareWrite(operation,args);
    const {collection,id,etag} = args;
    const method = {create:'POST', update:'PATCH', markDeletion:'PATCH', remove:'DELETE'}[operation];
    const newObject = operation === 'create';
    const result = await request({path:newObject ? collection : collection + "(guid'" + id + "')", method,
      body, query:{$format:'json'}, headers:newObject ? {} : {'If-Match':etag}});
    const data = result.data?.d ?? result.data;
    return {status:result.status, data, etag:data?.DataVersion ?? null};
  }
  return {collections,describe,list,get,validateWrite,create:(a)=>mutate('create',a),update:(a)=>mutate('update',a),
    markDeletion:(a)=>mutate('markDeletion',a),remove:(a)=>mutate('remove',a)};
}
