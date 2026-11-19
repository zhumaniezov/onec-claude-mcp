import { Buffer } from 'node:buffer';
import { isIP } from 'node:net';

const mebibyte = 1024 * 1024;
const uncertain = ' Результат записи может быть неизвестен. Проверьте состояние в 1С перед повтором.';

function checkedInteger(value, min, max, name) {
  if (!Number.isSafeInteger(value) || value < min || value > max) throw new Error('Некорректное значение ' + name);
  return value;
}

// Compare decimal significands and exponents without expanding scientific notation.
// JSON numbers that cannot round-trip through JS Number are delivered as exact strings.
function decimalKey(token) {
  const [mantissa, exponent = '0'] = token.toLowerCase().split('e');
  const [integer, fraction = ''] = mantissa.replace(/^-/, '').split('.');
  const digits = (integer + fraction).replace(/^0+/, '');
  if (!digits) return '0';
  const significant = digits.replace(/0+$/, '');
  const power = Number(exponent) - fraction.length + digits.length - significant.length;
  return (token.startsWith('-') ? '-' : '') + significant + 'e' + power;
}

function exactNumbers(key, value, context) {
  if (typeof value !== 'number') return value;
  if (!context?.source) throw new Error('Требуется Node.js 22 с JSON.parse context.source.');
  if (!Number.isFinite(value) || (Number.isInteger(value) && !Number.isSafeInteger(value))) return context.source;
  return decimalKey(context.source) === decimalKey(String(value)) ? value : context.source;
}

async function readBody(response, limit) {
  if (!response.body) return '';
  const reader = response.body.getReader();
  let size = 0;
  const parts = [];
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > limit) {
        await reader.cancel();
        throw new Error('Размер ответа 1С превышает лимит ' + limit + ' байт.');
      }
      parts.push(value);
    }
    return new TextDecoder('utf-8', {fatal:true}).decode(Buffer.concat(parts, size));
  } finally {
    reader.releaseLock();
  }
}

function publicationUrl(baseUrl, allowInsecureHttp) {
  const root = new URL(baseUrl);
  const host = root.hostname;
  const loopback = host === 'localhost' || host === '[::1]' || (isIP(host) === 4 && host.startsWith('127.'));
  const localHttp = root.protocol === 'http:' && allowInsecureHttp === true && loopback;
  if (root.protocol !== 'https:' && !localHttp) throw new Error('Требуется HTTPS. Для локального теста разрешите HTTP на loopback.');
  if (root.username || root.password || root.search || root.hash) throw new Error('URL публикации должен содержать только адрес и путь.');
  root.pathname = root.pathname.replace(/\/+$/, '') + '/';
  return root;
}

// Callers supply a fixed relative service path; credentials never follow redirects.
export function createTransport({ baseUrl, username, password, token, allowInsecureHttp = false,
  timeoutMs = 15000, maxResponseBytes = 4 * mebibyte }) {
  const root = publicationUrl(baseUrl, allowInsecureHttp);
  checkedInteger(timeoutMs, 1, 300000, 'timeoutMs');
  checkedInteger(maxResponseBytes, 1, 128 * mebibyte, 'maxResponseBytes');
  if (token && (username || password)) throw new Error('Задайте один способ авторизации.');
  if (password && !username) throw new Error('Укажите имя пользователя для Basic-аутентификации.');
  if (username?.includes(':')) throw new Error('Basic-аутентификация не поддерживает двоеточие в имени пользователя.');
  let authorization;
  if (username) authorization = 'Basic ' + Buffer.from(username + ':' + (password ?? ''), 'utf8').toString('base64');
  if (token) authorization = 'Bearer ' + token;
  return async function request({ path, method = 'GET', query = {}, body, headers = {}, format = 'json', maxBytes = maxResponseBytes }) {
    checkedInteger(maxBytes, 1, 128 * mebibyte, 'maxBytes');
    if (typeof path !== 'string' || !path || path.startsWith('/') || /[\\:?#%\x00-\x20]/.test(path) ||
      path.split('/').some((segment) => !segment || segment === '.' || segment === '..')) {
      throw new Error('Недопустимый путь сервиса 1С.');
    }
    const url = new URL(path, root);
    if (url.origin !== root.origin || !url.pathname.startsWith(root.pathname)) throw new Error('Путь вне публикации.');
    // Some OData v3 hosts parse plus signs literally. Use percent-encoded spaces.
    url.search = Object.entries(query).map(([key,value]) => encodeURIComponent(key) + '=' + encodeURIComponent(value)).join('&');
    const requestHeaders = new Headers(headers);
    requestHeaders.set('Accept', format === 'text' ? 'application/xml' : 'application/json');
    if (authorization) requestHeaders.set('Authorization', authorization);
    let payload;
    if (body !== undefined) {
      payload = JSON.stringify(body);
      if (Buffer.byteLength(payload) > 4 * mebibyte) throw new Error('Размер запроса превышает 4 МиБ.');
      requestHeaders.set('Content-Type', 'application/json; charset=utf-8');
    }
    const write = method !== 'GET';
    let response;
    try {
      response = await fetch(url, {method, headers:requestHeaders, body:payload, redirect:'manual',
        signal:AbortSignal.timeout(timeoutMs)});
    } catch {
      throw new Error('Сетевая ошибка или таймаут при обращении к 1С.' + (write ? uncertain : ''));
    }
    if (!response.ok) {
      // Do not expose proxy pages, stack traces, or secrets from third-party error bodies.
      await response.body?.cancel().catch(() => {});
      const meanings = {401:'Проверьте авторизацию.',403:'Недостаточно прав.',404:'Объект или публикация не найдены.',
        412:'Версия объекта изменилась. Прочитайте объект повторно.'};
      const detail = meanings[response.status] ?? (response.status < 400 ? 'Переход по перенаправлению запрещён.' : 'Проверьте журнал 1С.');
      const error = new Error('1С HTTP ' + response.status + '. ' + detail + (write && response.status >= 500 ? uncertain : ''));
      error.status = response.status;
      throw error;
    }
    let data;
    try {
      const text = await readBody(response, maxBytes);
      if (format === 'text') data = text;
      else if (!text.trim()) data = null;
      else {
        try { data = JSON.parse(text, exactNumbers); }
        catch { throw new Error('Сервис вернул некорректный JSON. Проверьте адрес публикации и авторизацию.'); }
      }
    } catch (error) {
      throw new Error((error.message.startsWith('Размер') || error.message.startsWith('Сервис') ? error.message : 'Не удалось прочитать ответ 1С.') +
        (write ? uncertain : ''));
    }
    return {status:response.status, data, headers:response.headers};
  };
}
