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

test('packages only the selected private WSS origin, never local development routes', () => {
  const release = buildReleaseManifest(
    'wss://home-mac.example-tailnet.ts.net/glasses',
    sourceManifest,
  );

  assert.deepEqual(networkWhitelist(release), [
    'wss://home-mac.example-tailnet.ts.net',
  ]);
  assert.ok(
    networkWhitelist(sourceManifest).some((origin) => origin.startsWith('ws://')),
    'the checked-in development manifest keeps a local plain-WS route',
  );
});

test('keeps multiple selected WSS brain origins explicit and preserves other permissions', () => {
  const release = buildReleaseManifest(
    'wss://home-mac.example-tailnet.ts.net/glasses,wss://laptop-mac.example-tailnet.ts.net/glasses',
    sourceManifest,
  );

  assert.deepEqual(networkWhitelist(release), [
    'wss://home-mac.example-tailnet.ts.net',
    'wss://laptop-mac.example-tailnet.ts.net',
  ]);
  assert.equal(release.package_id, sourceManifest.package_id);
  assert.deepEqual(
    release.permissions.filter((permission) => permission.name !== 'network'),
    sourceManifest.permissions.filter((permission) => permission.name !== 'network'),
  );
});

test('does not produce a release manifest for an unreviewed Mini', () => {
  assert.throws(
    () => buildReleaseManifest(
      'wss://unknown-mini.example-tailnet.ts.net/glasses',
      sourceManifest,
    ),
    /not in app\.json/,
  );
});
