import { buildsProbe } from "@/lib/builds/api";

// Whether builds are on: 200 {enabled} either way. The pages ask once after loading (docs/builds.md).
export const GET = () => buildsProbe();
