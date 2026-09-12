const assert = require('node:assert/strict');
const fs = require('node:fs');
assert.equal(process.env.CI_FIXTURE, 'node');
if (process.env.CI_DATABASE_URL) {
  const database = new URL(process.env.CI_DATABASE_URL);
  assert.equal(database.hostname, '127.0.0.1');
  assert.equal(database.pathname, '/fixture_test');
}
fs.mkdirSync('reports', { recursive: true });
fs.writeFileSync('reports/junit.xml', '<testsuites><testsuite tests="1" failures="0"><testcase name="environment"/></testsuite></testsuites>');
