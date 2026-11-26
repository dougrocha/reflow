import { resolverStore } from "@/lib/resolver";
import { manifestIsReady, resolveRequest } from "@/lib/resolver/resolve";

// Does a PDF link have a ready conversion? The answer and its rules are in @/lib/resolver/resolve.

export const GET = (req: Request) => resolveRequest(req, { store: resolverStore(), isReady: (id) => manifestIsReady(id) });
