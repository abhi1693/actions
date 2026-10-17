'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

// This is runner IPC. Only the official toolkit may emit the add-mask command.
function registerProjectMasks(core, environment = process.env) {
  let directory;
  let filename;
  let descriptor;
  let failed = false;
  try {
    if (typeof core.setSecret !== 'function') throw new Error('Missing runner SDK');
    const root = fs.realpathSync(environment.RUNNER_TEMP);
    directory = fs.mkdtempSync(path.join(root, 'project-masks-'));
    fs.chmodSync(directory, 0o700);
    filename = path.join(directory, 'values.json');
    execFileSync('python3', [path.join(environment.TOOLS, 'project.py'), 'mask-file'], {
      env: { ...environment, PROJECT_MASK_FILE: filename },
      stdio: ['ignore', 'pipe', 'pipe'],
      maxBuffer: 65536,
      timeout: 30000,
    });
    descriptor = fs.openSync(
      filename,
      fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK,
    );
    const info = fs.fstatSync(descriptor);
    if (
      !info.isFile() ||
      (info.mode & 0o777) !== 0o600 ||
      info.uid !== process.getuid() ||
      info.size > 1024 * 1024
    ) {
      throw new Error('Invalid private mask transport');
    }
    const values = JSON.parse(fs.readFileSync(descriptor, 'utf8'));
    if (
      !Array.isArray(values) ||
      values.some((value) => typeof value !== 'string' || !value || value.includes('\0'))
    ) {
      throw new Error('Invalid mask values');
    }
    // Python already normalized scalars losslessly. Do not coerce or pre-escape:
    // core.setSecret applies the official percent/CR/LF escaping exactly once.
    for (const value of values) core.setSecret(value);
  } catch {
    failed = true;
  } finally {
    const cleanup = [
      () => descriptor !== undefined && fs.closeSync(descriptor),
      () => {
        if (!filename) return;
        try {
          fs.unlinkSync(filename);
        } catch (error) {
          if (error.code !== 'ENOENT') throw error;
        }
      },
      () => directory && fs.rmdirSync(directory),
    ];
    for (const operation of cleanup) {
      try {
        operation();
      } catch {
        failed = true;
      }
    }
  }
  // Child stderr/parse/SDK errors can contain private input. Never forward them.
  if (failed) throw new Error('Unable to register project secret masks');
}

module.exports = { registerProjectMasks };
