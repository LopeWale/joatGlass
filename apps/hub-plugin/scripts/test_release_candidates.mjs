import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  allowedNetworkOrigins,
  releaseCandidateUrls,
  validateReleaseCandidates,
} from './release_candidates.mjs';

// Release-candidate validation must exercise the checked-in source allowlist.
// `test_release_manifest.mjs` separately proves that the package narrows it
// to the exact WSS origins selected for a release.
const manifest = JSON.parse(
  readFileSync(new URL('../app.json', import.meta.url), 'utf8'),
);

test('accepts only actual explicitly whitelisted private G2 gateway URLs', () => {
  assert.deepEqual(
    validateReleaseCandidates(
      'wss://admins-mac-mini.tail3a3363.ts.net/glasses,wss://emmanuels-macbook-pro.tail3a3363.ts.net/glasses',
      manifest,
    ),
    [
      'wss://admins-mac-mini.tail3a3363.ts.net/glasses',
      'wss://emmanuels-macbook-pro.tail3a3363.ts.net/glasses',
    ],
  );
});

test('rejects raw development transport even when the manifest allows it', () => {
  assert.throws(
    () => validateReleaseCandidates('ws://emmanuels-macbook-pro.local:8787/glasses', manifest),
    /must use wss/,
  );
});

test('rejects an unreviewed origin, alternate path, or duplicate endpoint', () => {
  assert.throws(
    () => validateReleaseCandidates('wss://unknown.tail3a3363.ts.net/glasses', manifest),
    /not in app\.json/,
  );
  assert.throws(
    () => validateReleaseCandidates('wss://admins-mac-mini.tail3a3363.ts.net/other', manifest),
    /origin-local \/glasses URL/,
  );
  assert.throws(
    () => validateReleaseCandidates(
      'wss://admins-mac-mini.tail3a3363.ts.net/glasses,wss://admins-mac-mini.tail3a3363.ts.net/glasses',
      manifest,
    ),
    /duplicated/,
  );
});

test('rejects an absent or malformed network declaration', () => {
  assert.throws(() => releaseCandidateUrls(''), /non-empty/);
  assert.throws(() => allowedNetworkOrigins({}), /network permissions/);
  assert.throws(
    () => allowedNetworkOrigins({ permissions: [{ name: 'network', whitelist: ['ok', 4] }] }),
    /whitelist is invalid/,
  );
});

test('never echoes rejected credential-bearing candidate input', () => {
  assert.throws(
    () => validateReleaseCandidates(
      'wss://operator:private-token@admins-mac-mini.tail3a3363.ts.net/glasses',
      manifest,
    ),
    (error) => {
      assert.ok(error instanceof Error);
      assert.match(error.message, /origin-local \/glasses URL/);
      assert.doesNotMatch(error.message, /operator|private-token/);
      return true;
    },
  );
});
