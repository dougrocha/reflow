// A DOM for the tests of code that walks the transcript's HTML (find, the chunk loader). Bun's own
// fetch classes are kept: happy-dom's drop the request headers a browser forbids (Origin,
// Sec-Fetch-Site) that the API route tests send.
import { GlobalRegistrator } from "@happy-dom/global-registrator";

const kept = { fetch, Request, Response, Headers, FormData, Blob, File, URL, URLSearchParams,
  AbortController, AbortSignal, ReadableStream, TextEncoder, TextDecoder };
GlobalRegistrator.register();
Object.assign(globalThis, kept);
