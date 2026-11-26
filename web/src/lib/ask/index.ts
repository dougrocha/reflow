// The only module importable from outside lib/ask/ (an oxlint rule enforces it). Server only.

export { ask, cleanQuestion, clientOf, startAskOllama, statusOf } from "./ask";
export { askReady } from "./corpus";
export { ollamaHost } from "./config";
