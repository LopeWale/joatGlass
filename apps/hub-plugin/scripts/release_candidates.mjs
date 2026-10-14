#!/usr/bin/env node

/**
 * Validate the public, build-time WebSocket candidates that become part of a
 * release. Pairing tokens never pass through this script: it accepts only
 * endpoint URLs. Each builder names their own Mac(s) in their local config;
 * those candidates become the packaged allowlist (release_manifest.mjs).
 */

import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

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

/**
 * Even Hub package ids: reverse-domain, at least two segments, each segment a
 * lowercase letter followed by lowercase letters or digits (no hyphens or
 * underscores). Every builder needs their own; the Hub keys builds by it.
 */
export function validatePackageId(value) {
  if (typeof value !== 'string' || !/^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+$/.test(value)) {
    throw new Error('package id must be reverse-domain lowercase, e.g. com.yourname.joat');
  }
  if (value === 'com.example.joat') {
    throw new Error('choose your own package id; com.example.joat is the placeholder');
  }
  return value;
}

/**
 * A production Glasses package must explicitly choose one or more TLS
 * origins. The /glasses path is the only public JOAT gateway route; raw dev
 * URLs and arbitrary paths cannot leak into it.
 */
export function validateReleaseCandidates(value) {
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
    if (seen.has(url.href)) {
      throw new Error('release candidate is duplicated');
    }
    seen.add(url.href);
    return url.href;
  });
}

function isDirectExecution() {
  return process.argv[1] !== undefined
    && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
}

if (isDirectExecution()) {
  const [rawCandidates] = process.argv.slice(2);
  if (rawCandidates === undefined || process.argv.length !== 3) {
    console.error('usage: release_candidates.mjs <wss-url[,wss-url]>');
    process.exitCode = 64;
  } else {
    try {
      const candidates = validateReleaseCandidates(rawCandidates);
      console.log(`validated ${candidates.length} private release candidate(s)`);
    } catch (error) {
      const message = error instanceof Error ? error.message : 'unknown error';
      console.error(`release candidate validation failed: ${message}`);
      process.exitCode = 1;
    }
  }
}
