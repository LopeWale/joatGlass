import { ImageContainerProperty, RebuildPageContainer } from '@evenrealities/even_hub_sdk';
import { ContainerId, HeaderImageId } from './types';

/**
 * One header on every page (the wearer's ask, 2026-10-06: "why are we not
 * using a template for the top bar?"). The image pages (the Planning month,
 * the live map) always drew the compact spine: the clock and the page's
 * word in a small face, the mic and battery as icons. The firmware pages
 * drew a line of the firmware font instead, bigger and different. Now every
 * firmware page carries the same spine as two 288x24 images (the SDK caps
 * an image at 288 px wide), drawn by the same renderer code.
 *
 * Images cost a send each, so the header is the part that rarely changes:
 * both halves after a page rebuild, then only a half whose pixels changed
 * (a minute tick redraws the clock's half, a mic or battery change the
 * other). The page's text stays firmware text and keeps upgrading in place.
 */

/** The spine's band, as the image pages draw it (HUD_VISUAL_GEOMETRY). */
export const HEADER_IMAGE_HEIGHT = 24;
export const HEADER_IMAGE_WIDTH = 288;

/** Whether a page carries the image header. */
export function hasImageHeader(page: RebuildPageContainer): boolean {
  return (page.imageObject ?? []).some((image) => image.containerID === HeaderImageId.Left);
}

/**
 * The page with its one-line status text swapped for the two header images.
 * A page without a status line, or one already full of images (the
 * four-tile page draws its own spine), is returned as it is.
 */
export function withImageHeader(page: RebuildPageContainer): RebuildPageContainer {
  const texts = page.textObject ?? [];
  if (!texts.some((text) => text.containerID === ContainerId.StatusBar)) return page;
  const images = page.imageObject ?? [];
  if (images.length > 2 || hasImageHeader(page)) return page;
  const rest = texts.filter((text) => text.containerID !== ContainerId.StatusBar);
  const others = [...rest, ...(page.listObject ?? []), ...images];
  // zOrderIndex is all or nothing per page: when the page orders its
  // containers, the header halves take the next two places.
  const ordered = others.some((item) => item.zOrderIndex !== undefined);
  const top = Math.max(0, ...others.map((item) => item.zOrderIndex ?? 0));
  const half = (containerID: number, containerName: string, x: number, z: number) => new ImageContainerProperty({
    containerID, containerName, xPosition: x, yPosition: 0,
    width: HEADER_IMAGE_WIDTH, height: HEADER_IMAGE_HEIGHT,
    ...(ordered ? { zOrderIndex: z } : {}),
  });
  return new RebuildPageContainer({
    ...page,
    containerTotalNum: rest.length + (page.listObject?.length ?? 0) + images.length + 2,
    textObject: rest,
    imageObject: [
      ...images,
      half(HeaderImageId.Left, 'hdr-left', 0, top + 1),
      half(HeaderImageId.Right, 'hdr-right', HEADER_IMAGE_WIDTH, top + 2),
    ],
  });
}
