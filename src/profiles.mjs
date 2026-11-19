import { readFile } from 'node:fs/promises';
import { z } from 'zod';

const envName = z.string().regex(/^[A-Za-z_][A-Za-z0-9_]*$/);
const profileSchema = z.strictObject({
  publicationUrl: z.string().min(1),
  label: z.string().max(120).optional(),
  usernameEnv: envName.optional(),
  passwordEnv: envName.optional(),
  tokenEnv: envName.optional(),
  allowLoopbackHttp: z.boolean().default(false),
  writeCollections: z.array(z.string().min(1)).default([]),
  physicalDelete: z.boolean().default(false),
  timeoutMs: z.int().min(1000).max(120000).default(20000),
  maxResponseMiB: z.int().min(1).max(128).default(32)
});
const configSchema = z.strictObject({
  bases: z.record(z.string().regex(/^[A-Za-z0-9_-]{1,64}$/), profileSchema)
});

export function parseProfiles(raw) {
  const result = configSchema.safeParse(raw);
  if (!result.success) {
    const paths = result.error.issues.map(i => i.path.join('.') || 'bases');
    throw new Error('Проверьте поля файла профилей: ' + paths.join(', '));
  }
  const profiles = new Map();
  for (const [name, profile] of Object.entries(result.data.bases)) {
    let url;
    try { url = new URL(profile.publicationUrl); }
    catch { throw new Error('Некорректный publicationUrl в профиле ' + name); }
    if (url.username || url.password || url.search || url.hash) {
      throw new Error('publicationUrl должен содержать только адрес публикации и путь.');
    }
    const local = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
    if (url.protocol !== 'https:' && !(url.protocol === 'http:' && local && profile.allowLoopbackHttp)) {
      throw new Error('Нужен HTTPS. Для локального стенда разрешите allowLoopbackHttp.');
    }
    if (Boolean(profile.usernameEnv) !== Boolean(profile.passwordEnv) ||
        (profile.tokenEnv && profile.usernameEnv)) {
      throw new Error('Укажите пару usernameEnv/passwordEnv или tokenEnv в профиле ' + name);
    }
    if (profile.physicalDelete && !profile.writeCollections.length) {
      throw new Error('Для physicalDelete укажите writeCollections в профиле ' + name);
    }
    const root = url.href.replace(/\/+$/, '');
    profiles.set(name, {
      ...profile, name,
      odataUrl: root + '/odata/standard.odata',
      extensionUrl: root + '/hs/onec_mcp/v1/'
    });
  }
  if (!profiles.size) throw new Error('В файле профилей нужен хотя бы один элемент bases.');
  return profiles;
}

export async function loadProfiles(path) {
  if (!path) throw new Error('Передайте --config <путь к profiles.json> или переменную ONEC_MCP_CONFIG.');
  let raw;
  try { raw = JSON.parse((await readFile(path, 'utf8')).replace(/^\uFEFF/, '')); }
  catch { throw new Error('Не удалось прочитать JSON-файл профилей. Проверьте путь и синтаксис.'); }
  return parseProfiles(raw);
}

export function connectionOptions(profile, env = process.env) {
  const secret = name => {
    if (!name) return undefined;
    if (!env[name]) throw new Error('Задайте переменную окружения ' + name + ' для процесса Claude Code.');
    return env[name];
  };
  return {
    username: secret(profile.usernameEnv), password: secret(profile.passwordEnv),
    token: secret(profile.tokenEnv), allowWrites: profile.writeCollections.length > 0,
    allowPhysicalDelete: profile.physicalDelete, allowInsecureHttp: profile.allowLoopbackHttp,
    timeoutMs: profile.timeoutMs, maxResponseBytes: profile.maxResponseMiB * 1024 * 1024,
    maxMetadataBytes: profile.maxResponseMiB * 1024 * 1024
  };
}
