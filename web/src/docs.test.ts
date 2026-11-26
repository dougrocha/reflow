import { describe, expect, test } from "bun:test";
import { collectionOf } from "./docs";

describe("collectionOf", () => {
  test("names the collection by the source's top folder", () => {
    expect(collectionOf("azblue/site/hipaa-npp.pdf")).toBe("azblue");
    expect(collectionOf("azblue/ma-provider-directory-pima.pdf")).toBe("azblue");
    expect(collectionOf("docaccess/01-chem-math.pdf")).toBe("docaccess");
  });

  test("puts everything else in other", () => {
    expect(collectionOf("resnet.pdf")).toBe("other");
    expect(collectionOf("corpus/uscis-i9.pdf")).toBe("other");
    // Only the folder counts, not a file named like one.
    expect(collectionOf("azblue.pdf")).toBe("other");
    expect(collectionOf("corpus/docaccess/x.pdf")).toBe("other");
  });
});
