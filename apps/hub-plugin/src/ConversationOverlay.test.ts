import { describe, expect, it } from 'vitest';
import {
  ListContainerProperty,
  ListItemContainerProperty,
  RebuildPageContainer,
  TextContainerProperty,
  validateEvenHubPageContainer,
} from '@evenrealities/even_hub_sdk';
import { firmwareLineCount } from './FirmwareText';
import { OVERLAY_GAP, overlayConversation } from './ConversationOverlay';
import { conversationViewport } from './AssistantConversationSurface';
import { HudPageBuilder } from './HudPageBuilder';
import { ContainerId } from './types';

const builder = new HudPageBuilder();

function card(lines: number): RebuildPageContainer {
  const turns = [
    { role: 'user' as const, text: 'What time is my next meeting?' },
    { role: 'assistant' as const, text: Array.from({ length: lines }, () => 'Design review at 2:30.').join('\n') },
  ];
  return builder.assistantConversationLayout(conversationViewport({
    conversationId: 'c', revision: 1, state: 'ready', continueAfterMs: 60_000, turns, earlierTurnsOmitted: false,
  }), '9:41 · battery');
}

function top(page: RebuildPageContainer): number {
  return page.textObject!.find((text) => text.containerName === 'conversation')!.yPosition!;
}

const longBody = Array.from({ length: 30 }, (_, i) => `Line ${i} of the page underneath.`).join('\n');

function textPage(): RebuildPageContainer {
  return builder.textPageLayout(longBody, { status: '', hint: 'swipe: more · tap: reply' });
}

describe('overlayConversation', () => {
  it('leaves the box alone when there is no page to float over', () => {
    const box = card(1);
    expect(overlayConversation(box, undefined)).toBe(box);
  });

  it('keeps the page above the box, cut to end before its border, with nothing overflowing', () => {
    const box = card(1);
    const page = overlayConversation(box, textPage());
    const limit = top(box) - OVERLAY_GAP;
    const under = page.textObject!.filter((text) => !box.textObject!.some(
      (own) => own.containerName === text.containerName,
    ));
    expect(under.length).toBeGreaterThan(0);
    for (const text of under) {
      expect((text.yPosition ?? 0) + (text.height ?? 0)).toBeLessThanOrEqual(limit);
      expect(text.isEventCapture).toBe(0);
      const pad = text.paddingLength ?? 0;
      const border = text.borderWidth ?? 0;
      const capacity = Math.floor(((text.height ?? 0) - 2 * (pad + border)) / 27);
      expect(firmwareLineCount(text.content ?? ' ', (text.width ?? 0) - 2 * (pad + border)))
        .toBeLessThanOrEqual(Math.max(1, capacity));
    }
    // The hint row lies under the box: it gives way.
    expect(under.some((text) => text.containerName === 'hint')).toBe(false);
    // One status line, the box's own; one capture, the box.
    expect(page.textObject!.filter((text) => text.containerID === ContainerId.StatusBar)).toHaveLength(1);
    expect(page.textObject!.filter((text) => text.isEventCapture === 1).map((text) => text.containerName))
      .toEqual(['conversation']);
    expect(validateEvenHubPageContainer(page)).toEqual({ valid: true });
  });

  it('draws the page beneath the box and keeps every stacking index unique', () => {
    const box = card(1);
    const page = overlayConversation(box, textPage());
    const z = page.textObject!.map((text) => text.zOrderIndex ?? 0);
    expect(new Set(z).size).toBe(z.length);
    const boxZ = page.textObject!.filter((text) => box.textObject!.some(
      (own) => own.containerName === text.containerName,
    )).map((text) => text.zOrderIndex ?? 0);
    const pageZ = page.textObject!.filter((text) => !box.textObject!.some(
      (own) => own.containerName === text.containerName,
    )).map((text) => text.zOrderIndex ?? 0);
    expect(Math.max(...pageZ)).toBeLessThan(Math.min(...boxZ));
    expect(page.containerTotalNum).toBe(page.textObject!.length);
  });

  it('gives the page less room as the box grows', () => {
    const small = overlayConversation(card(1), textPage());
    const grown = overlayConversation(card(6), textPage());
    const body = (page: RebuildPageContainer) => page.textObject!.find((text) => text.containerName === 'main');
    expect(top(card(6))).toBeLessThan(top(card(1)));
    expect(body(grown)?.height ?? 0).toBeLessThan(body(small)?.height ?? 0);
  });

  it('stays within the page limits: eight text and list containers', () => {
    const many = new RebuildPageContainer({
      containerTotalNum: 7,
      textObject: Array.from({ length: 7 }, (_, i) => new TextContainerProperty({
        containerID: i + 2, containerName: `row${i}`, xPosition: 0, yPosition: 36 + i * 4, width: 200, height: 40,
        paddingLength: 2, borderWidth: 0, isEventCapture: i === 0 ? 1 : 0, content: `row ${i}`,
      })),
    });
    const page = overlayConversation(card(1), many);
    expect(page.textObject!.length + (page.listObject?.length ?? 0)).toBeLessThanOrEqual(8);
    expect(validateEvenHubPageContainer(page)).toEqual({ valid: true });
  });

  it('cuts a firmware list above the box and hands its events to the box', () => {
    const list = new RebuildPageContainer({
      containerTotalNum: 1,
      listObject: [new ListContainerProperty({
        containerID: ContainerId.List, containerName: 'list', xPosition: 0, yPosition: 36, width: 576, height: 216,
        paddingLength: 0, borderWidth: 0, isEventCapture: 1,
        itemContainer: new ListItemContainerProperty({ itemCount: 3, itemWidth: 0, isItemSelectBorderEn: 1, itemName: ['a', 'b', 'c'] }),
      })],
    });
    const box = card(1);
    const page = overlayConversation(box, list);
    const cut = page.listObject?.[0];
    expect(cut?.isEventCapture).toBe(0);
    expect((cut?.yPosition ?? 0) + (cut?.height ?? 0)).toBeLessThanOrEqual(top(box) - OVERLAY_GAP);
    expect(validateEvenHubPageContainer(page)).toEqual({ valid: true });
  });

  it('leaves an image page to the box alone: tiles cannot be cut without resending them', () => {
    const box = card(1);
    expect(overlayConversation(box, builder.visualLayout())).toBe(box);
  });
});
