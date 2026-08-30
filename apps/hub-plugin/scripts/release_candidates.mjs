#!/usr/bin/env node

/**
 * Validate the public, build-time WebSocket candidates that become part of a
 * release. Pairing tokens never pass through this script: it accepts only
 * endpoint URLs and validates them against the reviewed manifest allowlist.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

export function releaseCandidateUrls(value) {
  if (typeof value !== 'string') {
    throw new TypeError('release candidates must be a comma-separated string');
  }
  const candidates = value.split(',').map((candidate) => candidate.trim());
  if (candidates.length === 0 || candidates.some((candidate) => candidate.length === 0)) {
    throw new Error('at least one non-empty release candidate is required');
  }
  return candidates;
}

export function allowedNetworkOrigins(manifest) {
  if (!isRecord(manifest) || !Array.isArray(manifest.permissions)) {
    throw new Error('app manifest does not declare network permissions');
  }
  const network = manifest.permissions.find((permission) => (
    isRecord(permission) && permission.name === 'network'
  ));
  if (!isRecord(network) || !Array.isArray(network.whitelist)) {
    throw new Error('app manifest does not declare a network whitelist');
  }
  const origins = network.whitelist.filter((entry) => typeof entry === 'string');
  if (origins.length !== network.whitelist.length) {
    throw new Error('app manifest network whitelist is invalid');
  }
  return new Set(origins);
}

/**
 * A production Glasses package must explicitly choose one or more HTTPS/TLS
 * origins already approved by Even Hub. The /glasses path is the only public
 * JOAT gateway route; raw dev URLs and arbitrary paths cannot leak into it.
 */
export function validateReleaseCandidates(value, manifest) {
  const approvedOrigins = allowedNetworkOrigins(manifest);
  const seen = new Set();
  return releaseCandidateUrls(value).map((candidate) => {
    let url;
    try {
      url = new URL(candidate);
    } catch {
      // This is a public build-time setting, but never echo an invalid value:
      // a pasted credential-bearing URL must not be copied into a build log.
      throw new Error('release candidate is not a URL');
    }
    if (url.protocol !== 'wss:') {
      throw new Error('release candidate must use wss');
    }
    if (url.pathname !== '/glasses' || url.search || url.hash || url.username || url.password) {
      throw new Error('release candidate must be an origin-local /glasses URL');
    }
    if (!approvedOrigins.has(url.origin)) {
      throw new Error(`release candidate origin is not in app.json: ${url.origin}`);
    }
    if (seen.has(url.href)) {
      throw new Error('release candidate is duplicated');
    }
    seen.add(url.href);
    return url.href;
  });
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
  const [rawCandidates, manifestPath] = process.argv.slice(2);
  if (rawCandidates === undefined || manifestPath === undefined || process.argv.length !== 4) {
    console.error('usage: release_candidates.mjs <wss-url[,wss-url]> <app.json>');
    process.exitCode = 64;
  } else {
    try {
      const candidates = validateReleaseCandidates(rawCandidates, readManifest(manifestPath));
      console.log(`validated ${candidates.length} private release candidate(s)`);
    } catch (error) {
      const message = error instanceof Error ? error.message : 'unknown error';
      console.error(`release candidate validation failed: ${message}`);
      process.exitCode = 1;
    }
  }
}
