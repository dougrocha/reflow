// The inspector's panels, and their order. On desktop, Details is drawn above Transcript in the
// side column; this order is for the phone tabs and the menus.
export type PanelId = "page" | "transcript" | "details";
export const PANEL_ORDER: PanelId[] = ["page", "transcript", "details"];
export const PANEL_LABELS: Record<PanelId, string> = {
  page: "Page",
  transcript: "Transcript",
  details: "Details",
};
