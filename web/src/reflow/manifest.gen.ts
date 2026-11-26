// Generated from schema/manifest.schema.json by `cd web && bun run schema:gen`. Do not edit: edit the schema.

import type { Schema } from "./schema";

/** manifest.json of a published document: the index the web viewer reads (render/manifest.py, make_manifest). The source of truth for both sides: web/src/reflow/manifest.gen.ts is generated from it (cd web && bun run schema:gen), and tests/test_viewer_contract.py checks that Python's output validates against it. */
export type PublishedManifest = {
  /** The digest of the published output (reflow.output.stamp_version), which the viewer appends to asset URLs; the format number 2 when the converter wrote no digest. */
  v: string | number;
  title: string;
  /** Present when the converter found no title in the document and the title is the PDF's file name (analyze.title); the viewer then does not set it as a heading. */
  titleFromFileName?: true;
  /** Present when page 1 opens with a title the converter found on it; the viewer then does not print the title above the page. */
  titleOnPage?: true;
  /** BCP 47 language of the document. */
  lang: string;
  /** Text direction of the whole document. */
  dir: "ltr" | "rtl";
  /** The language of a right-to-left document's left-to-right blocks, when they read clearly as one language. */
  ltrLang?: string;
  pageCount: number;
  /** The most common page size in points, [width, height]; pages list the pages that differ. */
  pageSize: Size;
  /** A page's printed label is its number plus this offset, unless pages gives its label. */
  labelOffset: number;
  /** Widths in px of the page images written, ascending. The first is pages/NNNN.webp, each other pages/NNNN-<width>.webp. Absent when the document has no page images. */
  imageWidths?: number[];
  /** The pages that differ from pageSize or labelOffset, keyed by page number. */
  pages: {
    [key: string]: PageOverride;
  };
  chunks: ManifestChunk[];
  /** The contents: top-level sections only when tocComplete is false (the full list is in toc.json). */
  toc: TocItem[];
  /** The base of the geometric scale the text counts are stored on: count = textBase^step - 1. */
  textBase: number;
  /** Each page's transcript size, in page order: [characters, blocks, lines], each a step on the textBase scale. */
  text: TextSize[];
  tocComplete: boolean;
  /** The original PDF has named AcroForm fields, and the transcript's controls say which. */
  fillable?: true;
  /** Fonts whose text layer read as other letters, decoded from OCR of the page images (the font-map review item): how many fonts and character codes. */
  decoded?: {
    fonts: number;
    codes: number;
  };
};

export type Size = [number, number];

export type PageOverride = {
  /** Width in points. */
  w?: number;
  /** Height in points. */
  h?: number;
  /** The label the page prints. */
  label?: string;
};

/** One chunk page of the transcript: chunks/NNNN.html holds pages first to last. */
export type ManifestChunk = {
  href: string;
  first: number;
  last: number;
};

export type TocItem = {
  level: number;
  text: string;
  /** Chunk href and the heading's anchor: chunks/0002.html#n12-ab12cd. */
  href: string;
  page: number;
};

export type TextSize = [number, number, number];

export const MANIFEST_SCHEMA: Schema = {
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://reflow.local/schema/manifest.schema.json",
  "title": "PublishedManifest",
  "description": "manifest.json of a published document: the index the web viewer reads (render/manifest.py, make_manifest). The source of truth for both sides: web/src/reflow/manifest.gen.ts is generated from it (cd web && bun run schema:gen), and tests/test_viewer_contract.py checks that Python's output validates against it.",
  "type": "object",
  "required": [
    "v",
    "title",
    "lang",
    "dir",
    "pageCount",
    "pageSize",
    "labelOffset",
    "pages",
    "chunks",
    "toc",
    "textBase",
    "text",
    "tocComplete"
  ],
  "additionalProperties": false,
  "properties": {
    "v": {
      "description": "The digest of the published output (reflow.output.stamp_version), which the viewer appends to asset URLs; the format number 2 when the converter wrote no digest.",
      "type": [
        "string",
        "integer"
      ]
    },
    "title": {
      "type": "string"
    },
    "titleFromFileName": {
      "description": "Present when the converter found no title in the document and the title is the PDF's file name (analyze.title); the viewer then does not set it as a heading.",
      "const": true
    },
    "titleOnPage": {
      "description": "Present when page 1 opens with a title the converter found on it; the viewer then does not print the title above the page.",
      "const": true
    },
    "lang": {
      "description": "BCP 47 language of the document.",
      "type": "string"
    },
    "dir": {
      "description": "Text direction of the whole document.",
      "enum": [
        "ltr",
        "rtl"
      ]
    },
    "ltrLang": {
      "description": "The language of a right-to-left document's left-to-right blocks, when they read clearly as one language.",
      "type": "string"
    },
    "pageCount": {
      "type": "integer",
      "minimum": 0
    },
    "pageSize": {
      "description": "The most common page size in points, [width, height]; pages list the pages that differ.",
      "$ref": "#/$defs/Size"
    },
    "labelOffset": {
      "description": "A page's printed label is its number plus this offset, unless pages gives its label.",
      "type": "integer"
    },
    "imageWidths": {
      "description": "Widths in px of the page images written, ascending. The first is pages/NNNN.webp, each other pages/NNNN-<width>.webp. Absent when the document has no page images.",
      "type": "array",
      "items": {
        "type": "integer",
        "minimum": 1
      },
      "minItems": 1
    },
    "pages": {
      "description": "The pages that differ from pageSize or labelOffset, keyed by page number.",
      "type": "object",
      "propertyNames": {
        "pattern": "^[1-9][0-9]*$"
      },
      "additionalProperties": {
        "$ref": "#/$defs/PageOverride"
      }
    },
    "chunks": {
      "type": "array",
      "items": {
        "$ref": "#/$defs/ManifestChunk"
      }
    },
    "toc": {
      "description": "The contents: top-level sections only when tocComplete is false (the full list is in toc.json).",
      "type": "array",
      "items": {
        "$ref": "#/$defs/TocItem"
      }
    },
    "textBase": {
      "description": "The base of the geometric scale the text counts are stored on: count = textBase^step - 1.",
      "type": "number",
      "exclusiveMinimum": 1
    },
    "text": {
      "description": "Each page's transcript size, in page order: [characters, blocks, lines], each a step on the textBase scale.",
      "type": "array",
      "items": {
        "$ref": "#/$defs/TextSize"
      }
    },
    "tocComplete": {
      "type": "boolean"
    },
    "fillable": {
      "description": "The original PDF has named AcroForm fields, and the transcript's controls say which.",
      "const": true
    },
    "decoded": {
      "description": "Fonts whose text layer read as other letters, decoded from OCR of the page images (the font-map review item): how many fonts and character codes.",
      "type": "object",
      "additionalProperties": false,
      "required": [
        "fonts",
        "codes"
      ],
      "properties": {
        "fonts": {
          "type": "integer",
          "minimum": 1
        },
        "codes": {
          "type": "integer",
          "minimum": 1
        }
      }
    }
  },
  "$defs": {
    "Size": {
      "type": "array",
      "prefixItems": [
        {
          "type": "number"
        },
        {
          "type": "number"
        }
      ],
      "items": false,
      "minItems": 2
    },
    "PageOverride": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "w": {
          "description": "Width in points.",
          "type": "number"
        },
        "h": {
          "description": "Height in points.",
          "type": "number"
        },
        "label": {
          "description": "The label the page prints.",
          "type": "string"
        }
      }
    },
    "ManifestChunk": {
      "description": "One chunk page of the transcript: chunks/NNNN.html holds pages first to last.",
      "type": "object",
      "required": [
        "href",
        "first",
        "last"
      ],
      "additionalProperties": false,
      "properties": {
        "href": {
          "type": "string"
        },
        "first": {
          "type": "integer",
          "minimum": 1
        },
        "last": {
          "type": "integer",
          "minimum": 1
        }
      }
    },
    "TocItem": {
      "type": "object",
      "required": [
        "level",
        "text",
        "href",
        "page"
      ],
      "additionalProperties": false,
      "properties": {
        "level": {
          "type": "integer",
          "minimum": 1
        },
        "text": {
          "type": "string"
        },
        "href": {
          "description": "Chunk href and the heading's anchor: chunks/0002.html#n12-ab12cd.",
          "type": "string"
        },
        "page": {
          "type": "integer",
          "minimum": 1
        }
      }
    },
    "TextSize": {
      "type": "array",
      "prefixItems": [
        {
          "type": "integer",
          "minimum": 0
        },
        {
          "type": "integer",
          "minimum": 0
        },
        {
          "type": "integer",
          "minimum": 0
        }
      ],
      "items": false,
      "minItems": 3
    }
  }
};
