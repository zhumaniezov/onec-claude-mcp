import test from 'node:test';
import assert from 'node:assert/strict';
import { parseProfiles, connectionOptions } from '../src/profiles.mjs';

function config(fields = {}) {
  return { bases: { demo: { publicationUrl: 'https://example.test/base/', ...fields } } };
}
test('profile defaults to read only and builds URLs relative to the infobase', () => {
  const profile = parseProfiles(config()).get('demo');
  assert.deepEqual(profile.writeCollections, []);
  assert.equal(profile.odataUrl, 'https://example.test/base/odata/standard.odata');
  assert.equal(profile.extensionUrl, 'https://example.test/base/hs/onec_mcp/v1/');
});
test('rejects remote HTTP, credentials in URLs, and flags accidentally supplied as strings', () => {
  for (const fields of [{publicationUrl:'http://example.test/base', allowLoopbackHttp:true},
    {publicationUrl:'https://user:pass@example.test/base'}, {physicalDelete:'false'}, {password:'public'}]) {
    assert.throws(() => parseProfiles(config(fields)));
  }
});
test('resolves auth from the environment only on access and refuses missing credentials', () => {
  const profile = parseProfiles(config({ usernameEnv:'DEMO_USER', passwordEnv:'DEMO_PASS' })).get('demo');
  assert.throws(() => connectionOptions(profile, {}), /DEMO_USER/);
  const options = connectionOptions(profile, {DEMO_USER:'u',DEMO_PASS:'p'});
  assert.equal(options.username, 'u');
  assert.equal(options.password, 'p');
});
