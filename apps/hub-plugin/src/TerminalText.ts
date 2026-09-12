/** Preserve terminal spacing and indentation; a tab is always four spaces.
 * Soft wraps keep every character, including the separating whitespace. */
export function wrapTerminalParagraph(text: string, width: number, measure: (text: string) => number): string[] {
  const points = Array.from(text.replace(/\t/g, '    '));
  if (!points.length) return [''];
  const rows: string[] = [];
  let offset = 0;
  while (offset < points.length) {
    const remaining = points.length - offset;
    const fits = (count: number) => measure(points.slice(offset, offset + count).join('')) <= width;
    let low = 0;
    let high = 1;
    while (high < remaining && fits(high)) {
      low = high;
      high = Math.min(remaining, high * 2);
    }
    if (fits(high)) low = high;
    else {
      while (high - low > 1) {
        const middle = Math.floor((high + low) / 2);
        if (fits(middle)) low = middle;
        else high = middle;
      }
    }
    let count = Math.max(1, low);
    if (count < remaining) {
      // Prefer a word boundary, but never turn indentation into a row by
      // itself. Unbroken identifiers use the measured code-point boundary.
      let hasText = false;
      let boundary = 0;
      for (let index = 0; index < count; index++) {
        if (/\s/.test(points[offset + index]!)) {
          if (hasText) boundary = index + 1;
        } else hasText = true;
      }
      if (boundary) count = boundary;
    }
    rows.push(points.slice(offset, offset + count).join(''));
    offset += count;
  }
  return rows;
}
