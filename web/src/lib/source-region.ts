// "Show on page": which part of a page image a transcript section came from. A plain module,
// shared by the server route (the types) and the viewer (the arithmetic).

/** One page's layout as /api/source sends it. Boxes are [x0, top, x1, bottom] in PDF points. */
export type PageSource = {
  width: number;
  height: number;
  /** The page's sections as the conversion wrote them: the heading's id and the section's box. */
  sections: { id: string; level: number; bbox: number[] }[];
};

export type SourceRegion = { bbox: [number, number, number, number]; width: number; height: number };

/**
 * The region of a section on its page. The conversion nests headings into SECTION nodes and
 * writes each one's box on the page into the page's layout, so this only looks it up. Null when
 * the heading opens no section on the page (output made before the conversion wrote them has
 * no `sections`; convert it again).
 */
export function sectionRegion(source: PageSource, headingId: string): SourceRegion | null {
  const section = source.sections?.find((s) => s.id === headingId);
  if (!section) return null;
  const [x0, top, x1, bottom] = section.bbox;
  return { bbox: [x0, top, x1, bottom], width: source.width, height: source.height };
}

/**
 * A box as percentages of its page, to lay over the page image at any size. The same scaling the
 * inspector uses for its boxes (PDF points to the image: x / page width, top / page height).
 */
export function boxPercent([x0, top, x1, bottom]: number[], width: number, height: number) {
  return {
    left: `${(x0 / width) * 100}%`,
    top: `${(top / height) * 100}%`,
    width: `${((x1 - x0) / width) * 100}%`,
    height: `${((bottom - top) / height) * 100}%`,
  };
}
