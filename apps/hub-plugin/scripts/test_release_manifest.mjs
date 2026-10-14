import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { buildReleaseManifest } from './release_manifest.mjs';

const sourceManifest = JSON.parse(
  readFileSync(new URL('../app.json', import.meta.url), 'utf8'),
);

function networkWhitelist(manifest) {
  const network = manifest.permissions.find((permission) => permission.name === 'network');
  assert.ok(network);
  return network.whitelist;
}

test('the checked-in manifest is generic: placeholder id, loopback simulator origins only', () => {
  assert.equal(sourceManifest.package_id, 'com.example.joat');
  for (const origin of networkWhitelist(sourceManifest)) {
    assert.match(origin, /^ws:\/\/(127\.0\.0\.1|localhost):8787$/);
  }
});

test('packages only the builder\'s selected WSS origin and package id', () => {
  const release = buildReleaseManifest(
    'wss://home-mac.example-tailnet.ts.net/glasses',
    'com.yourname.joat',
    sourceManifest,
  );

  assert.deepEqual(networkWhitelist(release), ['wss://home-mac.example-tailnet.ts.net']);
  assert.equal(release.package_id, 'com.yourname.joat');
  assert.equal(sourceManifest.package_id, 'com.example.joat', 'the source manifest is not mutated');
});

test('keeps multiple selected WSS origins explicit and preserves other permissions', () => {
  const release = buildReleaseManifest(
    'wss://home-mac.example-tailnet.ts.net/glasses,wss://laptop-mac.example-tailnet.ts.net/glasses',
    'com.yourname.joat',
    sourceManifest,
  );

  assert.deepEqual(networkWhitelist(release), [
    'wss://home-mac.example-tailnet.ts.net',
    'wss://laptop-mac.example-tailnet.ts.net',
  ]);
  assert.deepEqual(
    release.permissions.filter((permission) => permission.name !== 'network'),
    sourceManifest.permissions.filter((permission) => permission.name !== 'network'),
  );
});

test('refuses the placeholder package id or a plain-WS candidate', () => {
  assert.throws(
    () => buildReleaseManifest('wss://home-mac.example-tailnet.ts.net/glasses', 'com.example.joat', sourceManifest),
    /placeholder/,
  );
  assert.throws(
    () => buildReleaseManifest('ws://127.0.0.1:8787/glasses', 'com.yourname.joat', sourceManifest),
    /must use wss/,
  );
});
