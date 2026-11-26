import { enqueueBuild, listBuilds } from "@/lib/builds/api";

// Build tracking: the queue and recent history, and a new build. See docs/builds.md.
export const GET = (req: Request) => listBuilds(req);
export const POST = (req: Request) => enqueueBuild(req);
