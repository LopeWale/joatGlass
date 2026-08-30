import assert from 'node:assert/strict';
import test from 'node:test';

import {
  allowedNetworkOrigins,
  releaseCandidateUrls,
  validateReleaseCandidates,
} from './release_candidates.mjs';

const manifest = {
  permissions: [{
    name: 'network',
    whitelist: [
      'wss://home-mac.example-tailnet.ts.net',
      'wss://second-mini.example-tailnet.ts.net',
      'ws://laptop-mac.local:8787',
    ],
  }],
};

test('accepts explicitly whitelisted private G2 gateway URLs', () => {
  assert.deepEqual(
    validateReleaseCandidates(
      'wss://home-mac.example-tailnet.ts.net/glasses,wss://second-mini.example-tailnet.ts.net/glasses',
      manifest,
    ),
    [
      'wss://home-mac.example-tailnet.ts.net/glasses',
      'wss://second-mini.example-tailnet.ts.net/glasses',
    ],
  );
});

test('rejects raw development transport even when the manifest allows it', () => {
  assert.throws(
    () => validateReleaseCandidates('ws://laptop-mac.local:8787/glasses', manifest),
    /must use wss/,
  );
});

test('rejects an unreviewed origin, alternate path, or duplicate endpoint', () => {
  assert.throws(
    () => validateReleaseCandidates('wss://unknown.example-tailnet.ts.net/glasses', manifest),
    /not in app\.json/,
  );
  assert.throws(
    () => validateReleaseCandidates('wss://home-mac.example-tailnet.ts.net/other', manifest),
    /origin-local \/glasses URL/,
  );
  assert.throws(
    () => validateReleaseCandidates(
      'wss://home-mac.example-tailnet.ts.net/glasses,wss://home-mac.example-tailnet.ts.net/glasses',
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
      'wss://operator:private-token@home-mac.example-tailnet.ts.net/glasses',
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
