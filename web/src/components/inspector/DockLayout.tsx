"use client";

import "dockview-react/dist/styles/dockview.css";
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import {
  DockviewReact, themeLight,
  type DockviewApi, type DockviewReadyEvent, type IDockviewPanel, type IDockviewPanelHeaderProps,
} from "dockview-react";
import { Ellipsis } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { readStored, removeStored, writeStored } from "@/lib/storage";
import { PANEL_LABELS, PANEL_ORDER, type PanelId } from "./panels";

// The inspector's panels on a desktop, docked: drag a panel's tab to a side of another panel,
// or onto its tabs to share its place; the menu on each tab does the same from the keyboard. The
// layout is saved in the browser, and Reset layout goes back to the default: Page on the left,
// Details above Transcript on the right.

const DOCK_KEY = "reflow:inspect-dock";

/** What each panel shows: the workspace renders them, so they follow its state. */
const Contents = createContext<Record<PanelId, () => ReactNode> | null>(null);

const panelComponent = (id: PanelId) => function DockPanel() {
  const contents = useContext(Contents);
  return <div className="h-full min-h-0 overflow-hidden bg-background">{contents?.[id]()}</div>;
};
const COMPONENTS = Object.fromEntries(PANEL_ORDER.map((id) => [id, panelComponent(id)]));

/** dockview's light theme in the app's colours (.reflow-dock in globals.css): its class goes on the
 *  dock's outer shell, where the drop overlays draw too. */
const THEME = { ...themeLight, name: "reflow", className: `${themeLight.className} reflow-dock` };

const SIDES = [["left", "Move to the left"], ["right", "Move to the right"], ["top", "Move to the top"], ["bottom", "Move to the bottom"]] as const;

/** Move a panel to a side of the whole workspace, in a group of its own. */
function moveToSide(api: DockviewApi, panel: IDockviewPanel, side: (typeof SIDES)[number][0]): void {
  const group = api.addGroup({ direction: side });
  panel.api.moveTo({ group, position: "center" });
  panel.api.setActive();
}

/** Put a panel in another panel's place, as a tab beside it. */
function dockAsTab(panel: IDockviewPanel, with_: IDockviewPanel): void {
  panel.api.moveTo({ group: with_.group, position: "center" });
  panel.api.setActive();
}

/** A panel's tab: its title (the drag handle) and a menu that moves it from the keyboard. */
function Tab({ api, containerApi }: IDockviewPanelHeaderProps) {
  const id = api.id as PanelId;
  // The targets and which panels share a group are read when the menu opens: the tab does not
  // re-render when another panel moves.
  const [, reread] = useState(0);
  const panel = containerApi.getPanel(id);
  const others = PANEL_ORDER.filter((o) => o !== id && containerApi.getPanel(o));
  return (
    <div className="flex h-full items-center gap-1 pr-1 pl-3 type-small" data-dock-tab={id}>
      <span>{PANEL_LABELS[id]}</span>
      <DropdownMenu onOpenChange={(open) => { if (open) reread((n) => n + 1); }}>
        <DropdownMenuTrigger
          render={<Button variant="ghost" size="icon-xs" aria-label={`Move the ${PANEL_LABELS[id]} panel`} onPointerDown={(e) => e.stopPropagation()} />}
        >
          <Ellipsis aria-hidden="true" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-56">
          <DropdownMenuGroup>
            <DropdownMenuLabel>Move {PANEL_LABELS[id]} to…</DropdownMenuLabel>
            {SIDES.map(([side, label]) => (
              <DropdownMenuItem key={side} data-dock-move={side} onClick={() => panel && moveToSide(containerApi, panel, side)}>{label}</DropdownMenuItem>
            ))}
          </DropdownMenuGroup>
          {others.length ? <DropdownMenuSeparator /> : null}
          {others.length ? (
            <DropdownMenuGroup>
              <DropdownMenuLabel>Dock as tab</DropdownMenuLabel>
              {others.map((o) => {
                const other = containerApi.getPanel(o)!;
                const together = other.group.id === panel?.group.id;
                return (
                  <DropdownMenuItem key={o} data-dock-tab-with={o} disabled={together}
                                    onClick={() => panel && dockAsTab(panel, other)}>
                    With {PANEL_LABELS[o]}{together ? " (already)" : ""}
                  </DropdownMenuItem>
                );
              })}
            </DropdownMenuGroup>
          ) : null}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}

/** Add a panel where it sits by default, next to the panels already there. */
function addDefault(api: DockviewApi, id: PanelId): void {
  const base = { id, component: id, title: PANEL_LABELS[id] };
  if (id === "page") api.addPanel({ ...base, position: { direction: "left" } });
  else if (id === "details" && api.getPanel("transcript")) api.addPanel({ ...base, position: { referencePanel: "transcript", direction: "above" } });
  else if (id === "transcript" && api.getPanel("details")) api.addPanel({ ...base, position: { referencePanel: "details", direction: "below" } });
  else api.addPanel({ ...base, position: { direction: "right" } });
}

/** The default layout of the panels shown: Page 62% wide; Details 35% of the right column's height. */
function buildDefault(api: DockviewApi, visible: PanelId[]): void {
  api.clear();
  for (const id of PANEL_ORDER) if (visible.includes(id)) addDefault(api, id);
  // Sized once the dock has its size (on the first frame it may have none yet).
  requestAnimationFrame(() => {
    const page = api.getPanel("page");
    if (page && api.panels.length > 1) page.group.api.setSize({ width: Math.round(api.width * 0.62) });
    const details = api.getPanel("details");
    if (details && api.getPanel("transcript")) details.group.api.setSize({ height: Math.round(api.height * 0.35) });
  });
}

type SavedDock = Parameters<DockviewApi["fromJSON"]>[0];

/** A saved layout (DOCK_KEY's value), if it is ours: version 1, and only our panels in it. */
export function parseSavedDock(raw: string | null): SavedDock | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as { v?: unknown; layout?: { panels?: Record<string, unknown> } };
    if (parsed.v !== 1 || !parsed.layout?.panels) return null;
    if (!Object.keys(parsed.layout.panels).every((p) => (PANEL_ORDER as string[]).includes(p))) return null;
    return parsed.layout as SavedDock;
  } catch {
    return null;
  }
}

export function DockLayout({ visible, contents, resetSignal }: {
  /** The panels shown (the Panels menu); never empty. */
  visible: PanelId[];
  contents: Record<PanelId, () => ReactNode>;
  /** Changes when the user resets the layout. */
  resetSignal: number;
}) {
  const [api, setApi] = useState<DockviewApi | null>(null);
  const seenReset = useRef(resetSignal);

  function onReady(e: DockviewReadyEvent) {
    const saved = parseSavedDock(readStored(DOCK_KEY));
    let restored = false;
    if (saved) {
      try {
        e.api.fromJSON(saved);
        restored = true;
      } catch {
        removeStored(DOCK_KEY);
      }
    }
    if (!restored) buildDefault(e.api, visible);
    e.api.onDidLayoutChange(() => writeStored(DOCK_KEY, JSON.stringify({ v: 1, layout: e.api.toJSON() })));
    setApi(e.api);
  }

  // The Panels menu shows and hides panels: a hidden one leaves the layout, a shown one comes back
  // where it sits by default.
  useEffect(() => {
    if (!api) return;
    for (const id of PANEL_ORDER) {
      const panel = api.getPanel(id);
      if (visible.includes(id) && !panel) addDefault(api, id);
      if (!visible.includes(id) && panel) api.removePanel(panel);
    }
  }, [api, visible]);

  useEffect(() => {
    if (!api || seenReset.current === resetSignal) return;
    seenReset.current = resetSignal;
    removeStored(DOCK_KEY);
    buildDefault(api, PANEL_ORDER);
  }, [api, resetSignal]);

  return (
    <Contents.Provider value={contents}>
      <DockviewReact
        className="h-full"
        theme={THEME}
        components={COMPONENTS}
        defaultTabComponent={Tab}
        disableFloatingGroups
        onReady={onReady}
      />
    </Contents.Provider>
  );
}
