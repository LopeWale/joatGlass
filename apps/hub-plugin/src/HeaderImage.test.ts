import { describe, expect, it } from 'vitest';
import { validateEvenHubPageContainer } from '@evenrealities/even_hub_sdk';
import { HEADER_IMAGE_HEIGHT, hasImageHeader, withImageHeader } from './HeaderImage';
import { HudPageBuilder } from './HudPageBuilder';
import { conversationViewport } from './AssistantConversationSurface';
import { ContainerId, HeaderImageId, VisualContainerName } from './types';

const builder = new HudPageBuilder();

const PAGES = [
  ['text page', builder.textPageLayout('Body text', { status: '9:41 · MESSAGES', hint: 'tap: reply' })],
  ['list', builder.listLayout(['a', 'b', 'c'], { title: 'MESSAGES', status: '9:41 · MESSAGES' })],
  ['confirmation', builder.confirmationLayout('Send it?', '9:41 · JOAT')],
  ['conversation', builder.assistantConversationLayout(conversationViewport({
    conversationId: 'c', revision: 1, state: 'ready', continueAfterMs: 60_000,
    turns: [{ role: 'user', text: 'Hi' }, { role: 'assistant', text: 'Hello.' }], earlierTurnsOmitted: false,
  }), '9:41 · JOAT')],
] as const;

describe('withImageHeader', () => {
  it.each(PAGES)('%s: swaps the status line for the two spine halves and stays valid', (_name, page) => {
    const swapped = withImageHeader(page);
    expect(hasImageHeader(swapped)).toBe(true);
    expect(swapped.textObject?.some((text) => text.containerID === ContainerId.StatusBar)).toBe(false);
    const halves = swapped.imageObject?.filter((image) => (
      image.containerID === HeaderImageId.Left || image.containerID === HeaderImageId.Right
    )) ?? [];
    expect(halves.map((image) => [image.xPosition, image.yPosition, image.width, image.height]))
      .toEqual([[0, 0, 288, HEADER_IMAGE_HEIGHT], [288, 0, 288, HEADER_IMAGE_HEIGHT]]);
    expect(halves.map((image) => image.containerName))
      .toEqual([VisualContainerName[HeaderImageId.Left], VisualContainerName[HeaderImageId.Right]]);
    expect(swapped.containerTotalNum).toBe(
      (swapped.textObject?.length ?? 0) + (swapped.listObject?.length ?? 0) + (swapped.imageObject?.length ?? 0),
    );
    expect(validateEvenHubPageContainer(swapped)).toEqual({ valid: true });
  });

  it('leaves the four-tile page alone: it draws its own spine', () => {
    const visual = builder.visualLayout();
    expect(withImageHeader(visual)).toBe(visual);
  });

  it('is idempotent', () => {
    const once = withImageHeader(PAGES[0][1]);
    expect(withImageHeader(once)).toBe(once);
  });
});
