/**
 * Business view (the default) or expert tools. Business people see answers, dashboards, approvals and
 * plain words; the engineering surfaces (SQL console, notebooks, dbt builds, lineage graphs, agent
 * consoles, pilot scorecards) appear once someone turns expert tools on. Only what is shown changes:
 * every screen and URL still works and the server decides authority either way. Remembered per
 * browser in localStorage; without storage the choice lasts the tab.
 */
import { useSyncExternalStore } from "react";

const KEY = "analystos.expert";
let memory: boolean | null = null;
const listeners = new Set<() => void>();

export function getExpertView(): boolean {
  try {
    return window.localStorage.getItem(KEY) === "1";
  } catch {
    return memory ?? false;
  }
}

export function setExpertView(on: boolean): void {
  memory = on;
  try {
    if (on) window.localStorage.setItem(KEY, "1");
    else window.localStorage.removeItem(KEY);
  } catch {
    /* storage unavailable: keep the choice for this tab only */
  }
  listeners.forEach((l) => l());
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  const onStorage = (e: StorageEvent) => { if (e.key === KEY) cb(); };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(cb);
    window.removeEventListener("storage", onStorage);
  };
}

export function useExpertView(): boolean {
  return useSyncExternalStore(subscribe, getExpertView, () => false);
}
