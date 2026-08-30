#!/usr/bin/env node

/**
 * Build the manifest embedded in a deployable JOAT package.
 *
 * The checked-in manifest retains local-development origins so the official
 * simulator and QR loop can use an isolated plain-WS gateway. A release must
 * not inherit those origins: the selected private WSS candidates become the
 * entire network permission allowlist in the packaged manifest.
 */

import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { validateReleaseCandidates } from './release_candidates.mjs';

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

export function buildReleaseManifest(value, manifest) {
  if (!isRecord(manifest)) {
    throw new Error('app manifest must be an object');
  }
  // `validateReleaseCandidates` also makes this subset relationship explicit:
  // an unreviewed origin cannot be smuggled in through the release command.
  const candidates = validateReleaseCandidates(value, manifest);
  const releaseManifest = JSON.parse(JSON.stringify(manifest));
  const network = releaseManifest.permissions.find((permission) => (
    isRecord(permission) && permission.name === 'network'
  ));
  if (!isRecord(network)) {
    // Defensive only: validateReleaseCandidates already rejects this shape.
    throw new Error('app manifest does not declare network permissions');
  }
  network.whitelist = candidates.map((candidate) => new URL(candidate).origin);
  return releaseManifest;
}

function readManifest(path) {
  try {
    return JSON.parse(readFileSync(path, 'utf8'));
  } catch (error) {
    const message = error instanceof Error ? error.message : 'unknown error';
    throw new Error(`cannot read app manifest: ${message}`);
  }
}

function isDirectExecution() {
  return process.argv[1] !== undefined
    && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
}

if (isDirectExecution()) {
  const [rawCandidates, sourcePath, outputPath] = process.argv.slice(2);
  if (
    rawCandidates === undefined
    || sourcePath === undefined
    || outputPath === undefined
    || process.argv.length !== 5
  ) {
    console.error('usage: release_manifest.mjs <wss-url[,wss-url]> <app.json> <output.json>');
    process.exitCode = 64;
  } else {
    try {
      const manifest = buildReleaseManifest(rawCandidates, readManifest(sourcePath));
      writeFileSync(outputPath, `${JSON.stringify(manifest, null, 2)}\n`, { mode: 0o600 });
      console.log('wrote release manifest with selected private WSS origins');
    } catch (error) {
      const message = error instanceof Error ? error.message : 'unknown error';
      console.error(`release manifest generation failed: ${message}`);
      process.exitCode = 1;
    }
  }
}
