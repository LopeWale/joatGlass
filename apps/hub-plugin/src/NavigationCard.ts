import {
  ImageContainerProperty,
  RebuildPageContainer,
  TextContainerProperty,
} from '@evenrealities/even_hub_sdk';
import type { NavigationMapModel, NavigationMapPoint, NavigationMode } from './NavigationHud';
import { buildRouteSchematic, SCHEMATIC_SNAP_M } from './NavigationSchematic';
import { ContainerId, NavigationMapId } from './types';

/**
 * Hybrid active Navigation layout (mirrors HomeImages.ts and ReminderCard.ts):
 * - Left pane (`ContainerId.Main`): native firmware text for maneuver arrow,
 *   distance, street instruction, and ETA — updated in ~83 ms via in-place
 *   `textContainerUpgrade` on every GPS fix with zero image tiles sent.
 * - Top spine (`HeaderImage`): uniform borderless 24 px clock/page/mic/battery bar.
 * - Bottom bar (`ContainerId.Hint`): uniform firmware gesture hint.
 * - Right pane (`NavigationMapId`): one compact 264x144 image container holding
 *   the heading-up vector route schematic, quantized along the route so minor
 *   GPS ticks produce identical pixels and cost zero BLE image uploads.
 */

export const NAVIGATION_SCHEMATIC_CARD = {
  x: 296,
  y: 44,
  width: 264,
  height: 144,
} as const;

/** Left firmware text column beside the schematic: 16 px left inset (x=12 + 4 px pad). */
export const NAVIGATION_GUIDANCE_MAIN = {
  x: 12,
  width: 276,
  textWidth: 268,
} as const;

export interface NavigationSchematicView {
  map: NavigationMapModel;
  mode: NavigationMode;
  /** Under JOAT's floating conversation card: clip pixels at or below this page y. */
  clipBottom?: number;
  /** Under a popup notification card: clip pixels above this page y. */
  clipTop?: number;
  /** Dimmed beneath a notification card. */
  dim?: boolean;
}

const EARTH_RADIUS_M = 6_371_000;

const STEP_QUANTUM_M: Readonly<Record<NavigationMode, number>> = {
  walking: 12,
  cycling: 25,
  driving: 50,
};

const HEADING_QUANTUM_DEG = 10;

interface LocalXY {
  x: number;
  y: number;
}

function toLocal(origin: NavigationMapPoint, point: NavigationMapPoint): LocalXY {
  const lat0 = (origin.latitude * Math.PI) / 180;
  return {
    x: ((point.longitude - origin.longitude) * Math.PI / 180) * EARTH_RADIUS_M * Math.cos(lat0),
    y: ((point.latitude - origin.latitude) * Math.PI / 180) * EARTH_RADIUS_M,
  };
}

function fromLocal(origin: NavigationMapPoint, xy: LocalXY): NavigationMapPoint {
  const lat0 = (origin.latitude * Math.PI) / 180;
  const cosLat = Math.cos(lat0) || 1;
  return {
    latitude: origin.latitude + ((xy.y / EARTH_RADIUS_M) * 180) / Math.PI,
    longitude: origin.longitude + ((xy.x / (EARTH_RADIUS_M * cosLat)) * 180) / Math.PI,
  };
}

/**
 * Quantize a live `NavigationMapModel` along the route polyline so small GPS
 * steps (e.g. 2–8 m walking ticks) reuse the exact same projected schematic
 * pixels while firmware text updates live distance/ETA every second.
 */
export function quantizeNavigationSchematicView(
  map: NavigationMapModel | undefined,
  mode: NavigationMode = 'walking',
): NavigationSchematicView | undefined {
  if (!map || map.geometry.length < 2) return undefined;
  const localBounds = {
    x: 0,
    y: 0,
    width: NAVIGATION_SCHEMATIC_CARD.width,
    height: NAVIGATION_SCHEMATIC_CARD.height,
  };
  if (!buildRouteSchematic(map, localBounds, mode)) return undefined;

  const origin = map.geometry[0]!;
  const pts = map.geometry.map((p) => toLocal(origin, p));
  const along = [0];
  for (let i = 1; i < pts.length; i += 1) {
    along.push(along[i - 1]! + Math.hypot(pts[i]!.x - pts[i - 1]!.x, pts[i]!.y - pts[i - 1]!.y));
  }
  const totalM = along[along.length - 1] ?? 0;
  if (!(totalM > 0.5)) return undefined;

  const quantumM = STEP_QUANTUM_M[mode] ?? STEP_QUANTUM_M.walking;
  let quantizedPosition = map.position;
  let quantizedProgressM = map.progressM;

  if (map.position) {
    const fixXY = toLocal(origin, map.position);
    let bestCross = Infinity;
    let bestAlong = 0;
    for (let i = 0; i < pts.length - 1; i += 1) {
      const a = pts[i]!;
      const b = pts[i + 1]!;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const lenSq = dx * dx + dy * dy;
      const t = lenSq > 0
        ? Math.max(0, Math.min(1, ((fixXY.x - a.x) * dx + (fixXY.y - a.y) * dy) / lenSq))
        : 0;
      const px = a.x + dx * t;
      const py = a.y + dy * t;
      const cross = Math.hypot(fixXY.x - px, fixXY.y - py);
      if (cross < bestCross) {
        bestCross = cross;
        bestAlong = along[i]! + (along[i + 1]! - along[i]!) * t;
      }
    }

    if (bestCross <= SCHEMATIC_SNAP_M) {
      const sourceAlong = map.progressM !== undefined && Number.isFinite(map.progressM)
        ? Math.max(0, Math.min(totalM, map.progressM))
        : bestAlong;
      const snappedAlong = Math.min(totalM, Math.round(sourceAlong / quantumM) * quantumM);
      quantizedProgressM = snappedAlong;
      // Interpolate exact point on route at snappedAlong so origin stays identical within quantum!
      let stationXY = pts[pts.length - 1]!;
      if (snappedAlong <= 0) {
        stationXY = pts[0]!;
      } else {
        for (let i = 1; i < pts.length; i += 1) {
          if (along[i]! >= snappedAlong) {
            const segLen = along[i]! - along[i - 1]!;
            const ratio = segLen > 0 ? (snappedAlong - along[i - 1]!) / segLen : 0;
            stationXY = {
              x: pts[i - 1]!.x + (pts[i]!.x - pts[i - 1]!.x) * ratio,
              y: pts[i - 1]!.y + (pts[i]!.y - pts[i - 1]!.y) * ratio,
            };
            break;
          }
        }
      }
      quantizedPosition = fromLocal(origin, stationXY);
    } else {
      // Off route: quantize raw XY to quantumM grid so off-route drift doesn't spam BLE
      quantizedPosition = fromLocal(origin, {
        x: Math.round(fixXY.x / quantumM) * quantumM,
        y: Math.round(fixXY.y / quantumM) * quantumM,
      });
    }
  }

  const quantizedHeading = map.heading !== undefined && Number.isFinite(map.heading) && map.heading >= 0
    ? ((Math.round(map.heading / HEADING_QUANTUM_DEG) * HEADING_QUANTUM_DEG) % 360 + 360) % 360
    : undefined;

  return {
    mode,
    map: {
      geometry: map.geometry,
      ...(quantizedPosition ? { position: quantizedPosition } : {}),
      ...(quantizedHeading !== undefined ? { heading: quantizedHeading } : {}),
      ...(map.speed !== undefined ? { speed: map.speed >= 0.5 ? 1.5 : 0 } : {}),
      ...(quantizedProgressM !== undefined ? { progressM: quantizedProgressM } : {}),
      ...(map.maneuverAlongM !== undefined ? { maneuverAlongM: Math.round(map.maneuverAlongM) } : {}),
      ...(map.maneuverLabel ? { maneuverLabel: map.maneuverLabel } : {}),
    },
  };
}

/** Whether a firmware page carries the right-side Navigation schematic card. */
export function hasNavigationSchematic(page: RebuildPageContainer): boolean {
  return (page.imageObject ?? []).some((image) => image.containerID === NavigationMapId);
}

/**
 * Narrow a firmware text page's `Main` container to the left half (`x=12, width=276`)
 * and attach the single `264x144` right-side schematic image container (`NavigationMapId`).
 */
export function withNavigationSchematic(page: RebuildPageContainer): RebuildPageContainer {
  const texts = page.textObject ?? [];
  const main = texts.find((item) => item.containerID === ContainerId.Main);
  const images = page.imageObject ?? [];
  if (!main || images.length > 0 || hasNavigationSchematic(page)) return page;

  const narrowed = new TextContainerProperty({
    ...main,
    xPosition: NAVIGATION_GUIDANCE_MAIN.x,
    width: NAVIGATION_GUIDANCE_MAIN.width,
  });
  const nextTexts = texts.map((item) => (item === main ? narrowed : item));
  const others = [...nextTexts, ...(page.listObject ?? [])];
  const ordered = others.some((item) => item.zOrderIndex !== undefined);
  const top = Math.max(0, ...others.map((item) => item.zOrderIndex ?? 0));

  return new RebuildPageContainer({
    ...page,
    containerTotalNum: nextTexts.length + (page.listObject?.length ?? 0) + 1,
    textObject: nextTexts,
    imageObject: [
      new ImageContainerProperty({
        containerID: NavigationMapId,
        containerName: 'nav-map',
        xPosition: NAVIGATION_SCHEMATIC_CARD.x,
        yPosition: NAVIGATION_SCHEMATIC_CARD.y,
        width: NAVIGATION_SCHEMATIC_CARD.width,
        height: NAVIGATION_SCHEMATIC_CARD.height,
        ...(ordered ? { zOrderIndex: top + 1 } : {}),
      }),
    ],
  });
}
