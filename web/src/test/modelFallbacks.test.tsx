/**
 * N-14: an investigation step answered by rules because no model was allowed says so on the run view — one plain
 * sentence in business view, the router's detail only with expert tools on.
 */
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import type { ModelFallback } from "../api";
import { session } from "../api";
import { fallbackSentence, ModelFallbacks } from "../components/ModelFallbacks";
import { setExpertView } from "../lib/expert";
import { mockFetch, renderAt } from "./harness";
import { mockBackend, resetMockState, RUN, USER, WS } from "./mockBackend";

const FALLBACK: ModelFallback = {
  purpose: "hypothesis_generation", cause: "models_disabled", rung: "rules", code: "no_route",
  detail: "no allowed model for purpose 'hypothesis_generation' (profile reasoning_strong): the platform settings disable google/gemini-3.5-flash",
  message: "Answered by rules: no model is enabled for hypothesis generation.", count: 2, tasks: ["hypotheses"],
};

beforeEach(() => { resetMockState(); session.set("mock-token", USER); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); session.clear(); });

it("shows the plain sentence only in business view", () => {
  setExpertView(false);
  render(<ModelFallbacks fallbacks={[FALLBACK]} />);
  const box = screen.getByRole("group", { name: "Answered without a model" });
  expect(within(box).getByText("Answered by rules: no model is enabled for hypothesis generation.")).toBeTruthy();
  expect(screen.queryByRole("list", { name: "Model fallback details" })).toBeNull();
  expect(box.textContent).not.toContain("gemini");
});

it("adds the cause, steps and router detail with expert tools on", () => {
  setExpertView(true);
  render(<ModelFallbacks fallbacks={[FALLBACK]} />);
  const details = screen.getByRole("list", { name: "Model fallback details" });
  expect(details.textContent).toContain("models_disabled");
  expect(details.textContent).toContain("2 times");
  expect(details.textContent).toContain("steps hypotheses");
  expect(details.textContent).toContain("the platform settings disable google/gemini-3.5-flash");
});

it("renders nothing without fallbacks and builds a sentence when the server sent none", () => {
  const { container } = render(<ModelFallbacks fallbacks={[]} />);
  expect(container.textContent).toBe("");
  expect(fallbackSentence({ ...FALLBACK, message: "" })).toBe("Answered by rules: no model answered hypothesis generation.");
});

it("appears on the run view from the run detail", async () => {
  setExpertView(false);
  const detail = JSON.parse(mockBackend("GET", `/api/workspaces/${WS}/analysis/${RUN}`, null, {}).body);
  mockFetch((method, path) => method === "GET" && path === `/api/workspaces/${WS}/analysis/${RUN}`
    ? { status: 200, body: { ...detail, model_fallbacks: [FALLBACK] } } : null);
  renderAt(`/w/${WS}/work/investigations/${RUN}`);
  expect(await screen.findByText("Answered by rules: no model is enabled for hypothesis generation.")).toBeTruthy();
});
