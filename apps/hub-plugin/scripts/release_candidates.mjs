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

/** The brain on the phone itself: JOAT Brain (apps/ios-companion) listens on
 * ws://127.0.0.1:8787/glasses, the same loopback origin the simulator uses. */
export const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost']);

function isLoopbackCandidate(url) {
  return url.protocol === 'ws:' && LOOPBACK_HOSTS.has(url.hostname);
}

/**
 * A production Glasses package must explicitly choose one or more TLS
 * origins. The /glasses path is the only public JOAT gateway route; raw dev
 * URLs and arbitrary paths cannot leak into it.
 *
 * One exception, opt-in: with `allowLoopback` (JOAT_RELEASE_ALLOW_LOOPBACK=1)
 * a plain-ws loopback candidate is accepted for the iPhone-as-brain lab,
 * where the brain is a native app on the SAME phone as the Even app. Plain
 * ws is right there: nothing off the phone can reach 127.0.0.1, and TLS
 * would need a certificate the Even app's WebView trusts. Any other plain-ws
 * host is still refused, flag or not.
 */
export function validateReleaseCandidates(value, { allowLoopback = false } = {}) {
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
    if (isLoopbackCandidate(url)) {
      if (!allowLoopback) {
        throw new Error('release candidate must use wss (a phone-local ws://127.0.0.1 brain needs JOAT_RELEASE_ALLOW_LOOPBACK=1)');
      }
    } else if (url.protocol !== 'wss:') {
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
      const allowLoopback = process.env.JOAT_RELEASE_ALLOW_LOOPBACK === '1';
      const candidates = validateReleaseCandidates(rawCandidates, { allowLoopback });
      console.log(`validated ${candidates.length} private release candidate(s)${allowLoopback ? ' (loopback allowed: phone-local brain)' : ''}`);
    } catch (error) {
      const message = error instanceof Error ? error.message : 'unknown error';
      console.error(`release candidate validation failed: ${message}`);
      process.exitCode = 1;
    }
  }
}
