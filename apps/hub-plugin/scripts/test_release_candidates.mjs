import assert from 'node:assert/strict';
import test from 'node:test';

import {
  releaseCandidateUrls,
  validatePackageId,
  validateReleaseCandidates,
} from './release_candidates.mjs';

test('accepts private WSS /glasses gateway URLs in the order given', () => {
  assert.deepEqual(
    validateReleaseCandidates(
      'wss://home-mac.example-tailnet.ts.net/glasses,wss://laptop-mac.example-tailnet.ts.net/glasses',
    ),
    [
      'wss://home-mac.example-tailnet.ts.net/glasses',
      'wss://laptop-mac.example-tailnet.ts.net/glasses',
    ],
  );
});

test('rejects raw development transport', () => {
  assert.throws(
    () => validateReleaseCandidates('ws://laptop-mac.local:8787/glasses'),
    /must use wss/,
  );
});

test('rejects an alternate path or a duplicate endpoint', () => {
  assert.throws(
    () => validateReleaseCandidates('wss://home-mac.example-tailnet.ts.net/other'),
    /origin-local \/glasses URL/,
  );
  assert.throws(
    () => validateReleaseCandidates(
      'wss://home-mac.example-tailnet.ts.net/glasses,wss://home-mac.example-tailnet.ts.net/glasses',
    ),
    /duplicated/,
  );
});

test('rejects an empty candidate list', () => {
  assert.throws(() => releaseCandidateUrls(''), /non-empty/);
});

test('never echoes rejected credential-bearing candidate input', () => {
  assert.throws(
    () => validateReleaseCandidates(
      'wss://operator:private-token@home-mac.example-tailnet.ts.net/glasses',
    ),
    (error) => {
      assert.ok(error instanceof Error);
      assert.match(error.message, /origin-local \/glasses URL/);
      assert.doesNotMatch(error.message, /operator|private-token/);
      return true;
    },
  );
});

test('package ids follow the Even Hub reverse-domain rule and are never the placeholder', () => {
  assert.equal(validatePackageId('com.yourname.joat'), 'com.yourname.joat');
  for (const bad of ['joat', 'com.your-name.joat', 'com.YourName.joat', 'com.1st.joat', 'com.your_name.joat', '']) {
    assert.throws(() => validatePackageId(bad), /reverse-domain/, bad);
  }
  assert.throws(() => validatePackageId('com.example.joat'), /placeholder/);
});
