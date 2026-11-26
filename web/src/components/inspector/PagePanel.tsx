"use client";

import { memo, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { ChevronDown, Minus, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { PageImageSet } from "@/reflow/core";
import { styleFor, type InspectNode, type InspectTrace } from "@/lib/inspect";
import { boxPercent } from "@/lib/source-region";
import { typingIn } from "@/reflow/react/shortcuts";

export type Zoom = { fit: "width" | "page" | "custom"; percent: number };
const ZOOM_MIN = 50;
const ZOOM_MAX = 300;
const ZOOM_STEPS = [50, 75, 100, 125, 150, 200, 250, 300];

// The next zoom step up or down from `percent`.
function stepPercent(percent: number, dir: 1 | -1): number {
  if (dir > 0) return ZOOM_STEPS.find((s) => s > percent) ?? ZOOM_MAX;
  return [...ZOOM_STEPS].reverse().find((s) => s < percent) ?? ZOOM_MIN;
}

const PAD = 32; // px of breathing room around the page

// The page panel's surface size: the widest width that fits, then the zoom on top of it.
function displayWidth(zoom: Zoom, box: { w: number; h: number }, ratio: number) {
  const fitWidth = Math.max(0, box.w - PAD);
  const fitPage = Math.min(fitWidth, Math.max(0, box.h - PAD) * ratio);
  const width = zoom.fit === "page" ? fitPage : zoom.fit === "width" ? fitWidth : fitWidth * (zoom.percent / 100);
  return { width, fitWidth };
}

function useElementSize<T extends HTMLElement>(ref: { current: T | null }) {
  const [size, setSize] = useState({ w: 0, h: 0 });
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = () => setSize({ w: el.clientWidth, h: el.clientHeight });
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref]);
  return size;
}

export function PagePanel({
  title,
  pageText,
  image,
  width,
  height,
  nodes,
  trace,
  selected,
  suggestions,
  onSelect,
  hidden,
  onToggleRole,
  showLines,
  onToggleLines,
  zoom,
  setZoom,
}: {
  title: string;
  pageText: string;
  image: PageImageSet;
  width: number;
  height: number;
  nodes: InspectNode[];
  trace: InspectTrace | null;
  selected: string | null;
  /** The selected issue's OCR suggestions: each line's box is drawn over the page. */
  suggestions: { text: string; bbox: number[] | null }[];
  onSelect: (id: string) => void;
  hidden: Set<string>;
  onToggleRole: (role: string) => void;
  showLines: boolean;
  onToggleLines: () => void;
  zoom: Zoom;
  setZoom: (z: Zoom) => void;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const surface = useRef<HTMLDivElement>(null);
  const box = useElementSize(scroller);
  const ratio = width / height;
  const { width: surfaceWidth, fitWidth } = displayWidth(zoom, box, ratio);
  const percent = fitWidth > 0 ? Math.round((surfaceWidth / fitWidth) * 100) : 100;

  // Wheel with Ctrl or Cmd zooms around the cursor: the point under it stays under it.
  const pendingAnchor = useRef<{ x: number; y: number; cx: number; cy: number } | null>(null);
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      e.preventDefault();
      const s = surface.current;
      if (s) {
        const rect = el.getBoundingClientRect();
        const cx = e.clientX - rect.left;
        const cy = e.clientY - rect.top;
        const sRect = s.getBoundingClientRect();
        pendingAnchor.current = {
          x: (cx - (sRect.left - rect.left)) / sRect.width,
          y: (cy - (sRect.top - rect.top)) / sRect.height,
          cx,
          cy,
        };
      }
      const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
      const next = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, percent * factor));
      setZoom({ fit: "custom", percent: Math.round(next) });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [percent, setZoom]);

  // After a zoom step, put the scroll position back so the anchor point stays under the cursor.
  useLayoutEffect(() => {
    const a = pendingAnchor.current;
    const el = scroller.current;
    const s = surface.current;
    if (!a || !el || !s) return;
    pendingAnchor.current = null;
    const rect = el.getBoundingClientRect();
    const sRect = s.getBoundingClientRect();
    // The surface's position in scroll coordinates, now that it has its new size.
    const left = sRect.left - rect.left + el.scrollLeft;
    const top = sRect.top - rect.top + el.scrollTop;
    el.scrollLeft = left + a.x * sRect.width - a.cx;
    el.scrollTop = top + a.y * sRect.height - a.cy;
  }, [surfaceWidth]);

  // The selected box is kept in view (a click in the transcript can select a box off screen).
  useEffect(() => {
    if (!selected || !scroller.current) return;
    const el = scroller.current.querySelector<HTMLElement>(`[data-box="${selected}"]`);
    el?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [selected]);

  const stepZoom = (dir: 1 | -1) => setZoom({ fit: "custom", percent: stepPercent(percent, dir) });

  // + and - zoom the page. Ignored while typing in a field.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
      if (typingIn(e.target)) return;
      if (e.key === "+" || e.key === "=") stepZoomKey(1);
      else if (e.key === "-" || e.key === "_") stepZoomKey(-1);
      else return;
      e.preventDefault();
    };
    const stepZoomKey = (dir: 1 | -1) => setZoom({ fit: "custom", percent: stepPercent(percent, dir) });
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [percent, setZoom]);

  const shown = nodes.filter((n) => !hidden.has(n.role));
  const bySource: Record<string, number> = {};
  for (const n of shown) bySource[n.source] = (bySource[n.source] ?? 0) + 1;
  const roles = [...new Set(nodes.map((n) => n.role))];
  const options = ZOOM_STEPS.includes(percent) ? ZOOM_STEPS : [...ZOOM_STEPS, percent].sort((a, b) => a - b);

  return (
    <section aria-label={`Page ${pageText}`} className="flex h-full min-h-0 flex-col bg-muted/40">
      <header className="flex min-h-11 flex-wrap items-center gap-2 border-b bg-background px-3 py-1.5">
        <DropdownMenu>
          <DropdownMenuTrigger render={<Button variant="outline" />}>
            Layers
            <ChevronDown aria-hidden="true" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="min-w-64">
            <DropdownMenuGroup>
            <DropdownMenuLabel>Roles</DropdownMenuLabel>
            {roles.map((role) => {
              const style = styleFor(role);
              return (
                <DropdownMenuCheckboxItem
                  key={role}
                  checked={!hidden.has(role)}
                  onCheckedChange={() => onToggleRole(role)}
                >
                  <span aria-hidden="true" className="inline-block size-3 shrink-0 rounded-sm ring-1 ring-foreground/25" style={{ backgroundColor: style.color }} />
                  {style.label}
                  <span className="ml-auto pl-4 text-muted-foreground tabular-nums">
                    {nodes.filter((n) => n.role === role).length}
                  </span>
                </DropdownMenuCheckboxItem>
              );
            })}
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuCheckboxItem checked={showLines} disabled={!trace} onCheckedChange={onToggleLines}>
              Text lines
              <span className="ml-auto pl-4 text-muted-foreground tabular-nums">{trace ? trace.lines.length : "no trace"}</span>
            </DropdownMenuCheckboxItem>
          </DropdownMenuContent>
        </DropdownMenu>

        <div className="ml-auto flex items-center gap-1.5">
          <Button variant={zoom.fit === "width" ? "secondary" : "outline"} aria-pressed={zoom.fit === "width"}
                  onClick={() => setZoom({ fit: "width", percent: 100 })}>
            Fit width
          </Button>
          <Button variant={zoom.fit === "page" ? "secondary" : "outline"} aria-pressed={zoom.fit === "page"}
                  onClick={() => setZoom({ fit: "page", percent: 100 })}>
            Fit page
          </Button>
          <Button variant="outline" size="icon" aria-label="Zoom out" onClick={() => stepZoom(-1)} disabled={percent <= ZOOM_MIN}>
            <Minus />
          </Button>
          <label className="sr-only" htmlFor="zoom-select">Zoom</label>
          <select
            id="zoom-select"
            value={String(percent)}
            onChange={(e) => setZoom({ fit: "custom", percent: Number(e.target.value) })}
            className="h-8 rounded-lg border border-input bg-transparent px-2 text-sm tabular-nums outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring dark:bg-input/30"
          >
            {options.map((o) => (
              <option key={o} value={o}>{o}%</option>
            ))}
          </select>
          <Button variant="outline" size="icon" aria-label="Zoom in" onClick={() => stepZoom(1)} disabled={percent >= ZOOM_MAX}>
            <Plus />
          </Button>
        </div>
      </header>

      {/* The gutter is always reserved: fit width reads clientWidth, and a scrollbar that
          comes and goes would resize the page, toggle the scrollbar again, and loop. */}
      <div ref={scroller} className="min-h-0 flex-1 overflow-auto p-4 [scrollbar-gutter:stable]">
        <div
          ref={surface}
          className="relative mx-auto overflow-hidden rounded-md bg-white shadow-sm ring-1 ring-foreground/10"
          style={{
            width: surfaceWidth,
            aspectRatio: `${width} / ${height}`,
            // Room to scroll past the page's far edges, so a point near the bottom or right can
            // stay under the cursor while zooming. Only when the page overflows, so a page that
            // fits stays centred.
            marginRight: surfaceWidth + PAD > box.w ? box.w : undefined,
            marginBottom: box.h,
          }}
        >
          <PageImage key={image.src} image={image} sizes={`${Math.round(surfaceWidth)}px`} alt={`Page ${pageText} of ${title}`} />
          {trace && showLines ? <LinesLayer trace={trace} width={width} height={height} /> : null}
          <BoxLayer nodes={shown} width={width} height={height} selected={selected} onSelect={onSelect} />
          <SuggestionLayer suggestions={suggestions} width={width} height={height} />
        </div>
      </div>

      <footer className="border-t bg-background px-3 py-2 text-right type-small text-muted-foreground">
        {shown.length} of {nodes.length} boxes shown
        {Object.keys(bySource).length > 0 ? ` · engines: ${Object.entries(bySource).map(([s, n]) => `${s} ${n}`).join(", ")}` : ""}
        {trace && showLines ? ` · reading order: ${trace.split}` : null}
      </footer>
    </section>
  );
}

// The image, or a muted message in the same box when it cannot be loaded (for example while
// the document is being rebuilt). The boxes stay drawn either way.
function PageImage({ image, sizes, alt }: { image: PageImageSet; sizes: string; alt: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <div className="absolute inset-0 grid place-items-center p-6 text-center text-sm text-muted-foreground">
        Page image unavailable (the document may be rebuilding)
      </div>
    );
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element -- static page images in public/, sized by the overlay
    <img {...image} sizes={sizes} alt={alt} onError={() => setFailed(true)} className="absolute inset-0 size-full" />
  );
}

const LinesLayer = memo(function LinesLayer({ trace, width, height }: { trace: InspectTrace; width: number; height: number }) {
  return (
    <>
      {trace.gutters.map(([lo, hi]) => (
        <div key={`gutter-${lo}`} aria-hidden="true" className="pointer-events-none absolute inset-y-0 bg-yellow-500/15"
             style={{ left: `${(lo / width) * 100}%`, width: `${((hi - lo) / width) * 100}%` }} />
      ))}
      {trace.lines.map((line, i) => {
        return (
          <div key={`line-${i}`} aria-hidden="true"
               className={line.artifact
                 ? "pointer-events-none absolute border border-dashed border-slate-400"
                 : "pointer-events-none absolute border border-slate-500/70"}
               style={boxPercent(line.bbox, width, height)} />
        );
      })}
    </>
  );
});

// The lines that hold the selected issue's OCR suggestions, drawn over the boxes. Not a control:
// the issue's own box selects it.
function SuggestionLayer({ suggestions, width, height }: { suggestions: { text: string; bbox: number[] | null }[]; width: number; height: number }) {
  return (
    <>
      {suggestions.map((s, i) => {
        if (!s.bbox) return null;
        return (
          <div key={`suggestion-${i}`} data-suggestion={s.text} aria-hidden="true"
               className="pointer-events-none absolute z-[3] border-2 border-dashed"
               style={{
                 // Drawn on the white page image in both themes, like the boxes: 6.5:1 on white.
                 borderColor: "#b91c1c",
                 ...boxPercent(s.bbox, width, height),
               }} />
        );
      })}
    </>
  );
}

// The boxes. Memoized: a zoom step or a keypress does not re-render them, only a change of
// nodes, layers or selection does.
const BoxLayer = memo(function BoxLayer({
  nodes,
  width,
  height,
  selected,
  onSelect,
}: {
  nodes: InspectNode[];
  width: number;
  height: number;
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <>
      {nodes.map((node) => (
        <BoxButton key={node.id} node={node} width={width} height={height} active={node.id === selected} onSelect={onSelect} />
      ))}
    </>
  );
});

const BoxButton = memo(function BoxButton({
  node,
  width,
  height,
  active,
  onSelect,
}: {
  node: InspectNode;
  width: number;
  height: number;
  active: boolean;
  onSelect: (id: string) => void;
}) {
  const style = styleFor(node.role);
  const preview = node.text.slice(0, 60) || node.id;
  const trigger: ReactNode = (
    <button
      type="button"
      data-box={node.id}
      aria-label={`${style.label}: ${preview}`}
      aria-pressed={active}
      onClick={() => onSelect(node.id)}
      className="absolute cursor-pointer focus-visible:outline-[3px] focus-visible:outline-offset-2 focus-visible:outline-ring"
      style={{
        ...boxPercent(node.bbox, width, height),
        // Selection is near-black, offset from the box, so it differs from the orange focus ring.
        border: `${active ? 3 : 1.5}px solid ${style.color}`,
        backgroundColor: active ? `${style.color}26` : undefined,
        outline: active ? "2px solid #111827" : undefined,
        outlineOffset: active ? 2 : undefined,
        zIndex: active ? 2 : undefined,
      }}
    />
  );
  return (
    <Tooltip>
      <TooltipTrigger render={trigger} />
      <TooltipContent side="top" className="max-w-sm">
        <span className="font-medium">{style.label}</span>
        <span className="opacity-80">{preview}</span>
      </TooltipContent>
    </Tooltip>
  );
});

