import { describe, expect, it, vi } from 'vitest';
import {
  replaySyntheticAudioFixture,
  syntheticAudioFixtureUrl,
  type SyntheticAudioSink,
} from './SyntheticAudioFixture';

describe('syntheticAudioFixtureUrl', () => {
  it('maps a token to the loopback Vite public fixture directory', () => {
    expect(
      syntheticAudioFixtureUrl('http://127.0.0.1:5195/?joatAudioFixture=hermes-90e42249')
        ?.href,
    ).toBe(
      'http://127.0.0.1:5195/__joat-audio-fixture/hermes-90e42249.pcm',
    );
  });

  it('refuses non-loopback pages and path-like fixture identities', () => {
    expect(
      syntheticAudioFixtureUrl('https://hub.example/?joatAudioFixture=hermes-90e42249'),
    ).toBeUndefined();
    expect(() => (
      syntheticAudioFixtureUrl('http://localhost:5195/?joatAudioFixture=../../secret')
    )).toThrow('fixture token is invalid');
  });
});

describe('replaySyntheticAudioFixture', () => {
  it('replays signed 16-bit PCM at the production 200ms cadence', async () => {
    const chunks: number[] = [];
    const roles: Array<string | null | undefined> = [];
    const sink: SyntheticAudioSink = {
      isOpen: true,
      ingest: (pcm, role) => {
        chunks.push(pcm.length);
        roles.push(role);
      },
    };
    const waits: number[] = [];
    const fetchFixture = vi.fn(async () => (
      new Response(new Uint8Array(6_402), { status: 200 })
    ));

    const batches = await replaySyntheticAudioFixture(
      sink,
      new URL('http://127.0.0.1:5195/__joat-audio-fixture/test.pcm'),
      {
        fetch: fetchFixture as typeof fetch,
        wait: async (milliseconds) => { waits.push(milliseconds); },
      },
    );

    expect(batches).toBe(2);
    expect(chunks).toEqual([6_400, 2]);
    expect(roles).toEqual(['self', 'self']);
    expect(waits).toEqual([200, 200]);
  });

  it('refuses an unavailable mic lane and malformed PCM', async () => {
    const closed: SyntheticAudioSink = { isOpen: false, ingest: vi.fn() };
    await expect(replaySyntheticAudioFixture(
      closed,
      new URL('http://localhost/test.pcm'),
    )).rejects.toThrow('requires an open mic lane');

    const open: SyntheticAudioSink = { isOpen: true, ingest: vi.fn() };
    await expect(replaySyntheticAudioFixture(
      open,
      new URL('http://localhost/test.pcm'),
      { fetch: async () => new Response(new Uint8Array(3)) },
    )).rejects.toThrow('even-length PCM');
  });
});
