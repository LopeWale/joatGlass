const FIXTURE_PARAMETER = 'joatAudioFixture';
const FIXTURE_TOKEN = /^[a-z0-9][a-z0-9-]{0,63}$/;
const PCM_BYTES_PER_BATCH = 6_400;
const MAXIMUM_PCM_BYTES = 32_000 * 30;

export interface SyntheticAudioSink {
  readonly isOpen: boolean;
  ingest(pcm: Uint8Array, speakerRole?: string | null): void;
}

/**
 * Resolve a same-origin PCM fixture only for a loopback Vite page.
 *
 * The caller is additionally wrapped in `import.meta.env.DEV`, so this seam is
 * eliminated from the packed Hub plugin. A token maps to Vite's public test
 * directory; it is never interpreted as a caller-controlled filesystem path.
 */
export function syntheticAudioFixtureUrl(pageHref: string): URL | undefined {
  const page = new URL(pageHref);
  if (
    page.protocol !== 'http:'
    || (page.hostname !== '127.0.0.1' && page.hostname !== 'localhost')
  ) {
    return undefined;
  }
  const token = page.searchParams.get(FIXTURE_PARAMETER);
  if (token === null) return undefined;
  if (!FIXTURE_TOKEN.test(token)) {
    throw new Error('synthetic audio fixture token is invalid');
  }
  return new URL(`/__joat-audio-fixture/${token}.pcm`, page.origin);
}

export async function replaySyntheticAudioFixture(
  audio: SyntheticAudioSink,
  fixtureUrl: URL,
  options: {
    fetch?: typeof fetch;
    wait?: (milliseconds: number) => Promise<void>;
  } = {},
): Promise<number> {
  if (!audio.isOpen) throw new Error('synthetic audio fixture requires an open mic lane');
  const fetchFixture = options.fetch ?? fetch;
  const response = await fetchFixture(fixtureUrl);
  if (!response.ok) throw new Error(`synthetic audio fixture HTTP ${response.status}`);
  const pcm = new Uint8Array(await response.arrayBuffer());
  if (pcm.length === 0 || pcm.length % 2 !== 0 || pcm.length > MAXIMUM_PCM_BYTES) {
    throw new Error('synthetic audio fixture must be even-length PCM under 30 seconds');
  }
  const wait = options.wait ?? (
    (milliseconds: number) => new Promise<void>((resolve) => setTimeout(resolve, milliseconds))
  );
  let batches = 0;
  for (let offset = 0; offset < pcm.length; offset += PCM_BYTES_PER_BATCH) {
    audio.ingest(pcm.slice(offset, offset + PCM_BYTES_PER_BATCH), 'self');
    batches += 1;
    await wait(200);
  }
  return batches;
}
