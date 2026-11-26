import { NotFoundState } from "@/components/app/not-found-state";

// notFound() inside the site group renders here, within the (site) layout that already has
// the header, <main> and footer. The root not-found.tsx carries its own chrome for URLs that
// match no route at all.
export default function SiteNotFound() {
  return <NotFoundState />;
}
