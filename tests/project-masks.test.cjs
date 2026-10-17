'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { registerProjectMasks } = require('../.github/actions/workflow-tools/project-masks.cjs');

const tools = path.resolve(__dirname, '../.github/actions/workflow-tools');
function fixture(callback, privateInput = '{}') {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'project-mask-test-'));
  const environment = {
    ...process.env,
    TOOLS: tools,
    RUNNER_TEMP: root,
    PROJECT_ENV: '{"PUBLIC":"visible"}',
    PROJECT_SECRETS: privateInput,
  };
  try {
    callback(root, environment);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}
function fakeTools(root, body) {
  const directory = path.join(root, 'tools');
  fs.mkdirSync(directory);
  fs.writeFileSync(
    path.join(directory, 'project.py'),
    `import os, pathlib, sys\np=pathlib.Path(os.environ['PROJECT_MASK_FILE'])\n${body}\n`,
  );
  return directory;
}
function empty(root) {
  assert.deepEqual(
    fs.readdirSync(root).filter((name) => name.startsWith('project-masks-')),
    [],
  );
}

test('Python normalization reaches the official SDK losslessly and without pre-escaping', () => {
  const value = 'unicode-☃ %0A\r\n::warning::not-a-command';
  fixture(
    (root, environment) => {
      const masks = [];
      registerProjectMasks({ setSecret: (mask) => masks.push(mask) }, environment);
      assert.deepEqual(masks, ['1.0', '9007199254740993', 'true', value]);
      empty(root);
    },
    `{"LIMIT":1.0,"BIG":9007199254740993,"FLAG":true,"TOKEN":${JSON.stringify(value)},"EMPTY":"","UNSET":null}`,
  );
});

test('public values are not registered and empty/null secrets produce no SDK calls', () => {
  fixture((root, environment) => {
    registerProjectMasks({ setSecret: () => assert.fail('Unexpected mask') }, environment);
    empty(root);
  }, '{"EMPTY":"","UNSET":null}');
});

test('the bridge is exclusive and 0600 within a private 0700 runner directory', () => {
  fixture((root, environment) => {
    let called = false;
    registerProjectMasks(
      {
        setSecret() {
          called = true;
          const directory = fs.readdirSync(root).find((name) => name.startsWith('project-masks-'));
          assert.equal(fs.statSync(path.join(root, directory)).mode & 0o777, 0o700);
          assert.equal(fs.statSync(path.join(root, directory, 'values.json')).mode & 0o777, 0o600);
        },
      },
      environment,
    );
    assert.equal(called, true);
    empty(root);
  }, '{"TOKEN":"private-fixture"}');
});

test('malformed or unsafe inputs fail generically without exposing child stderr', () => {
  for (const input of [
    '{"TOKEN":"private-fixture",',
    '{"PATH":"private-fixture"}',
    '{"TOKEN":{"private":"fixture"}}',
  ]) {
    fixture((root, environment) => {
      assert.throws(
        () =>
          registerProjectMasks({ setSecret: () => assert.fail('Unexpected mask') }, environment),
        { message: 'Unable to register project secret masks' },
      );
      empty(root);
    }, input);
  }
});

test('SDK errors never expose values and still remove the bridge', () => {
  fixture((root, environment) => {
    assert.throws(
      () =>
        registerProjectMasks(
          {
            setSecret(value) {
              throw new Error(value);
            },
          },
          environment,
        ),
      { message: 'Unable to register project secret masks' },
    );
    empty(root);
  }, '{"TOKEN":"private-fixture"}');
});

test('failed preparation cannot leak captured stdout or stderr', () => {
  fixture((root, environment) => {
    environment.TOOLS = fakeTools(
      root,
      "print('private-fixture'); print('private-fixture', file=sys.stderr); sys.exit(1)",
    );
    assert.throws(
      () => registerProjectMasks({ setSecret: () => assert.fail('Unexpected mask') }, environment),
      { message: 'Unable to register project secret masks' },
    );
    empty(root);
  });
});

test('symlinks, FIFOs, permissive files and malformed mask arrays are rejected and cleaned', () => {
  const bodies = [
    "p.symlink_to('missing')",
    'os.mkfifo(p, 0o600)',
    'p.write_text(\'["private-fixture"]\'); p.chmod(0o644)',
    "p.write_text('{private-fixture'); p.chmod(0o600)",
    "p.write_text('[1]'); p.chmod(0o600)",
    'p.write_text(\'[""]\'); p.chmod(0o600)',
  ];
  for (const body of bodies) {
    fixture((root, environment) => {
      environment.TOOLS = fakeTools(root, body);
      assert.throws(
        () =>
          registerProjectMasks({ setSecret: () => assert.fail('Unexpected mask') }, environment),
        { message: 'Unable to register project secret masks' },
      );
      empty(root);
    });
  }
});

test('the actual Python exporter remains silent after the SDK has registered raw masks', () => {
  fixture((root, environment) => {
    const { execFileSync } = require('node:child_process');
    const value = 'private-fixture';
    const events = [];
    registerProjectMasks({ setSecret: (mask) => events.push(['mask', mask]) }, environment);
    const target = path.join(root, 'environment');
    const result = execFileSync('python3', [path.join(tools, 'project.py'), 'environment'], {
      env: { ...environment, GITHUB_ENV: target },
      encoding: 'utf8',
    });
    events.push(['export', fs.readFileSync(target, 'utf8')]);
    assert.deepEqual(events[0], ['mask', value]);
    assert.match(events[1][1], /private-fixture/);
    assert.equal(result, '');
    empty(root);
  }, '{"TOKEN":"private-fixture"}');
});

test('a close failure still attempts file and directory removal and blocks export', () => {
  fixture((root, environment) => {
    const close = fs.closeSync;
    let failed = false;
    fs.closeSync = (descriptor) => {
      close(descriptor);
      if (!failed) {
        failed = true;
        throw new Error('private-fixture');
      }
    };
    try {
      assert.throws(() => registerProjectMasks({ setSecret() {} }, environment), {
        message: 'Unable to register project secret masks',
      });
    } finally {
      fs.closeSync = close;
    }
    assert.equal(failed, true);
    empty(root);
  }, '{"TOKEN":"private-fixture"}');
});
