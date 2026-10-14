#!/usr/bin/env node

/**
 * Build the manifest embedded in a deployable JOAT package.
 *
 * The checked-in manifest is generic: a placeholder package id and only the
 * loopback origins the official simulator uses. A release is personal to
 * whoever builds it, so it takes that builder's package id and their own
 * private WSS candidates, which become the entire network allowlist.
 */

import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { validatePackageId, validateReleaseCandidates } from './release_candidates.mjs';

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

export function buildReleaseManifest(value, packageId, manifest) {
  if (!isRecord(manifest) || !Array.isArray(manifest.permissions)) {
    throw new Error('app manifest must declare permissions');
  }
  const candidates = validateReleaseCandidates(value);
  const releaseManifest = JSON.parse(JSON.stringify(manifest));
  releaseManifest.package_id = validatePackageId(packageId);
  const network = releaseManifest.permissions.find((permission) => (
    isRecord(permission) && permission.name === 'network'
  ));
  if (!isRecord(network)) {
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
  const [rawCandidates, packageId, sourcePath, outputPath] = process.argv.slice(2);
  if (outputPath === undefined || process.argv.length !== 6) {
    console.error('usage: release_manifest.mjs <wss-url[,wss-url]> <package-id> <app.json> <output.json>');
    process.exitCode = 64;
  } else {
    try {
      const manifest = buildReleaseManifest(rawCandidates, packageId, readManifest(sourcePath));
      writeFileSync(outputPath, `${JSON.stringify(manifest, null, 2)}\n`, { mode: 0o600 });
      console.log(`wrote release manifest for ${manifest.package_id} with selected private WSS origins`);
    } catch (error) {
      const message = error instanceof Error ? error.message : 'unknown error';
      console.error(`release manifest generation failed: ${message}`);
      process.exitCode = 1;
    }
  }
}
