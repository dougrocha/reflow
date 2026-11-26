"use client";

import dynamic from "next/dynamic";
import type { ComponentType } from "react";
import { useBuilds } from "./context";

// The build controls the document pages show. Pages import them from here: their code (buttons,
// icons, status) is fetched only once the page has learnt that builds are on, so with builds off a
// page loads none of it (docs/builds.md, The gate). Each renders nothing while builds are off, and
// nothing until its code has arrived.

function whenOn<P extends object>(Control: ComponentType<P>) {
  return function WhenOn(props: P) {
    return useBuilds() ? <Control {...props} /> : null;
  };
}

export const RebuildButton = whenOn(dynamic(() => import("./rebuild-button").then((m) => m.RebuildButton), { ssr: false }));
export const ConvertNow = whenOn(dynamic(() => import("./rebuild-button").then((m) => m.ConvertNow), { ssr: false }));
export const DecisionsSection = whenOn(dynamic(() => import("@/components/document/decisions-list").then((m) => m.DecisionsSection), { ssr: false }));
export const DecidedBadge = whenOn(dynamic(() => import("@/components/document/decisions-list").then((m) => m.DecidedBadge), { ssr: false }));
export const BuildStatus = whenOn(dynamic(() => import("./build-status").then((m) => m.BuildStatus), { ssr: false }));
export const BuildsLink = whenOn(dynamic(() => import("./builds-link").then((m) => m.BuildsLink), { ssr: false }));
