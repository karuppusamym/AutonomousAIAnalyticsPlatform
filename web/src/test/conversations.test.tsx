/**
 * S11-T2 on the Ask screen: every answer names who answered and what it cost, a branch keeps the original and shows
 * where it came from, typed corrections and their promotion, and state and memory chips (personal versus team visibly
 * distinct). The v2 conversation API is mocked over the in-memory backend.
 */
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import axe from "axe-core";
import { session, type AnswerCost } from "../api";
import { answeredByLabel, branchPointBefore, costLine, MEMORY_LABELS } from "../lib/ask";
import { bodyOf, calls, mockFetch, renderAt } from "./harness";
import { askTurn, THREAD_NEW, THREAD_OLD, USER, WS } from "./mockBackend";

vi.setConfig({ testTimeout: 20000 });

beforeEach(() => session.set("mock-token", USER));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

const COST: AnswerCost = { usd: 0.0052, model_calls: 2, cache_hits: 0, avoided: 1, tokens_saved: 600, rungs: ["llm_small", "llm_large"],
  models: ["small-1", "large-1"] };
const OLD = askTurn("askt_old", "How many P1 incidents per week?", { thread_id: THREAD_OLD, cost: COST, model: "small-1" });
const THREAD = { id: THREAD_OLD, workspace_id: WS, user_id: USER.id, title: "Weekly P1 volume", archived: false,
  created_at: "2026-09-20T09:00:00Z", updated_at: "2026-09-20T09:00:00Z", turn_count: 1 };
const conversationView = (id: string, over: Record<string, unknown> = {}) => ({
  ...THREAD, id, tenant_id: "tnt_default", channel: "ui", owner_id: USER.id, participants: [USER.id], request_id: null,
  parent_conversation_id: null, branch_from_message_id: null, legal_hold: false, closed_at: null, summarized_at: null, revision: 1,
  state_version: 2, message_count: 1, messages: [], ...over,
});
const EMPTY_STATE = { filters: [], sources: [], metric_bindings: {}, time_window: null, clarifications: [], result_handles: [] };

it("names who answered and what it cost, in words", () => {
  expect(answeredByLabel({ answered_by: "registry", model: null, status: "answered", route: "verified_query" }))
    .toBe("verified query (no model)");
  expect(answeredByLabel({ answered_by: "model", model: "small-1", status: "answered", route: "generate", cost: COST }))
    .toBe("model small-1 (small model, large model)");
  expect(answeredByLabel({ answered_by: null, model: null, status: "delegated", route: "investigate" })).toBe("an investigation run");
  expect(costLine(COST)).toBe("$0.0052 · 2 model calls · 1 model call avoided");
  expect(costLine({ ...COST, usd: 0, model_calls: 0, avoided: 0, cache_hits: 1 })).toBe("$0 · no model call · 1 from cache");
  expect(costLine(undefined)).toBe("cost not recorded");
  expect(branchPointBefore([{ id: "a" }, { id: "b" }], "b")).toBe("a");
  expect(branchPointBefore([{ id: "a" }, { id: "b" }], "a")).toBeNull();
  expect(new Set(Object.values(MEMORY_LABELS).map((m) => m.className)).size).toBe(3);  // personal and team never look alike
});

it("shows state chips and memory chips, with personal and team meanings labelled apart", async () => {
  const fetchMock = mockFetch((m, p, body) => {
    if (m === "GET" && p === `/api/ask/threads/${THREAD_OLD}`) return { status: 200, body: { ...THREAD, turns: [OLD] } };
    if (m === "GET" && p === `/api/v2/conversations/${THREAD_OLD}/state`) {
      return { status: 200, body: { conversation_id: THREAD_OLD, version: 3, at_seq: 1, cause: "correction", message_id: null,
        state: { ...EMPTY_STATE, filters: [{ column: "priority", op: "=", value: "P1" }], time_window: "last 30 days", sources: ["stg_sn.incident"] } } };
    }
    if (m === "GET" && p === `/api/v2/conversations/${THREAD_OLD}/memory`) {
      return { status: 200, body: { items: [
        { term: "open", term_key: "open", meaning: "state is not Closed", correction_id: "corr_s", scope: "session" },
        { term: "active", term_key: "active", meaning: "paid in 90 days", correction_id: "corr_p", scope: "personal" },
        { term: "churn", term_key: "churn", meaning: "no order in 180 days", correction_id: "corr_t", scope: "workspace" },
      ], overrides: [{ term: "active", used: "personal", meaning: "paid in 90 days", team_meaning: "logged in within 30 days",
        team_correction_id: "corr_x" }] } };
    }
    if (m === "GET" && p === `/api/v2/conversations/${THREAD_OLD}/corrections`) {
      return { status: 200, body: [{ id: "corr_s", conversation_id: THREAD_OLD, message_id: null, user_id: USER.id, term: "open",
        meaning: "state is not Closed", scope: "session", status: "active", promoted_from: null, suggestion_id: null, expires_at: null,
        created_at: null }] };
    }
    if (m === "POST" && /\/corrections\/corr_s\/promote$/.test(p)) {
      return { status: 201, body: { correction: { id: "corr_p2", scope: JSON.parse(body ?? "{}").to }, state_version: 4 } };
    }
    if (m === "PATCH" && p === `/api/v2/conversations/${THREAD_OLD}/state`) return { status: 200, body: { conversation_id: THREAD_OLD } };
    return null;
  });
  const { container } = renderAt(`/w/${WS}/ask?thread=${THREAD_OLD}`);
  const answeredBy = await screen.findByLabelText("Answered by");
  expect(answeredBy.textContent).toContain("model small-1 (small model, large model)");
  expect(answeredBy.textContent).toContain("$0.0052 · 2 model calls");

  const state = await screen.findByLabelText(/Conversation state, version 3/);
  expect(within(state).getByText(/Filter: priority = P1/)).toBeTruthy();
  expect(within(state).getByText(/Period: last 30 days/)).toBeTruthy();
  const memory = screen.getByLabelText("Meanings the next answer will use");
  const chips = within(memory).getAllByRole("listitem");
  expect(chips.map((c) => c.querySelector(".memory-scope")?.textContent)).toEqual(["This conversation", "Personal", "Team"]);
  expect(chips[1].className).toContain("memory-personal");
  expect(chips[2].className).toContain("memory-team");
  expect(screen.getByText(/not the team definition/)).toBeTruthy();  // never silent

  fireEvent.click(within(chips[0]).getByRole("button", { name: "Remember" }));
  await waitFor(() => expect(calls(fetchMock, "POST", /corrections\/corr_s\/promote$/)).toHaveLength(1));
  expect(bodyOf(calls(fetchMock, "POST", /corrections\/corr_s\/promote$/)[0][1])).toEqual({ to: "personal" });

  fireEvent.click(within(state).getByRole("button", { name: "Remove filter priority" }));
  await waitFor(() => expect(calls(fetchMock, "PATCH", /\/state$/)).toHaveLength(1));
  expect(bodyOf(calls(fetchMock, "PATCH", /\/state$/)[0][1])).toEqual({ filters: [] });

  const results = await axe.run(container.querySelector(".conversation-bar") as Element, { rules: { "color-contrast": { enabled: false } } });
  expect(results.violations).toEqual([]);
});

it("applies a typed correction with the chosen scope", async () => {
  const fetchMock = mockFetch((m, p, body) => {
    if (m === "POST" && p === `/api/v2/conversations/${THREAD_OLD}/corrections`) {
      return { status: 201, body: { correction: { id: "corr_new", ...JSON.parse(body ?? "{}"), status: "proposed" }, state_version: 1 } };
    }
    return null;
  });
  renderAt(`/w/${WS}/ask?thread=${THREAD_OLD}`);
  fireEvent.click(await screen.findByText("Correct a term"));
  const form = screen.getByRole("form", { name: "Correct a term" });
  fireEvent.change(within(form).getByLabelText("Term"), { target: { value: "active customer" } });
  fireEvent.change(within(form).getByLabelText("Means"), { target: { value: "logged in within 30 days" } });
  fireEvent.click(within(form).getByLabelText(/^Propose as the team definition/));
  fireEvent.click(within(form).getByRole("button", { name: "Apply correction" }));
  await waitFor(() => expect(calls(fetchMock, "POST", /\/corrections$/)).toHaveLength(1));
  expect(bodyOf(calls(fetchMock, "POST", /\/corrections$/)[0][1]))
    .toEqual({ term: "active customer", meaning: "logged in within 30 days", scope: "workspace_proposed" });
  // the memory is read again after the change
  await waitFor(() => expect(calls(fetchMock, "GET", new RegExp(`/v2/conversations/${THREAD_OLD}/memory$`)).length).toBeGreaterThan(1));
});

it("branches from an answer, says where the branch came from and keeps the original", async () => {
  const fetchMock = mockFetch((m, p) => {
    if (m === "POST" && p === `/api/v2/conversations/${THREAD_OLD}/branch`) {
      return { status: 201, body: { ...conversationView(THREAD_NEW, { parent_conversation_id: THREAD_OLD, branch_from_message_id: "askt_old",
        title: "Weekly P1 volume (branch)" }), restored_state: { conversation_id: THREAD_OLD, version: 2 } } };
    }
    if (m === "GET" && p === `/api/ask/threads/${THREAD_NEW}`) {
      return { status: 200, body: { ...THREAD, id: THREAD_NEW, title: "Weekly P1 volume (branch)", parent_conversation_id: THREAD_OLD, turns: [] } };
    }
    if (m === "GET" && p === `/api/v2/conversations/${THREAD_NEW}`) {
      return { status: 200, body: conversationView(THREAD_NEW, { parent_conversation_id: THREAD_OLD, branch_from_message_id: "askt_old",
        messages: [{ id: "askt_old", conversation_id: THREAD_OLD, seq: 1, inherited: true, author_id: USER.id, text: OLD.question,
          mode: "ask", status: "answered", created_at: OLD.created_at, answer: OLD }] }) };
    }
    return null;
  });
  renderAt(`/w/${WS}/ask?thread=${THREAD_OLD}`);
  fireEvent.click(await screen.findByRole("button", { name: "Branch from here" }));
  await waitFor(() => expect(screen.getByTestId("location").textContent).toContain(`thread=${THREAD_NEW}`));
  expect(bodyOf(calls(fetchMock, "POST", /\/branch$/)[0][1])).toEqual({ from_message_id: "askt_old" });
  expect(await screen.findByText(/The original conversation is unchanged/)).toBeTruthy();
  expect(screen.getByText(/after question 1/)).toBeTruthy();
  expect(await screen.findByLabelText("Question 1, from the original conversation")).toBeTruthy();
});

it("edits a question by asking it in a new branch before it", async () => {
  const fetchMock = mockFetch((m, p) => {
    if (m === "POST" && p === `/api/v2/conversations/${THREAD_OLD}/branch`) {
      return { status: 201, body: { ...conversationView(THREAD_NEW, { parent_conversation_id: THREAD_OLD }),
        restored_state: { conversation_id: THREAD_OLD, version: 0 } } };
    }
    if (m === "GET" && p === `/api/ask/threads/${THREAD_NEW}`) {
      return { status: 200, body: { ...THREAD, id: THREAD_NEW, parent_conversation_id: THREAD_OLD, turns: [] } };
    }
    return null;
  });
  renderAt(`/w/${WS}/ask?thread=${THREAD_OLD}`);
  fireEvent.click(await screen.findByRole("button", { name: "Edit question" }));
  fireEvent.change(screen.getByLabelText("Edited question"), { target: { value: "How many P2 incidents per week?" } });
  fireEvent.click(screen.getByRole("button", { name: "Ask in a new branch" }));
  await waitFor(() => expect(calls(fetchMock, "POST", new RegExp(`/ask/threads/${THREAD_NEW}/turns$`))).toHaveLength(1));
  expect(bodyOf(calls(fetchMock, "POST", /\/branch$/)[0][1])).toEqual({ from_message_id: null });  // before the first question
  expect(bodyOf(calls(fetchMock, "POST", /\/turns$/)[0][1]).question).toBe("How many P2 incidents per week?");
  expect(calls(fetchMock, "POST", new RegExp(`/ask/threads/${THREAD_OLD}/turns$`))).toHaveLength(0);  // the original is untouched
});

it("shows a meaning remembered in this conversation as personal memory, with Forget", async () => {
  const fetchMock = mockFetch((m, p) => {
    if (m === "GET" && p === `/api/v2/conversations/${THREAD_OLD}/memory`) {
      return { status: 200, body: { items: [{ term: "resolved", term_key: "resolved", meaning: "Resolved or Closed", correction_id: "corr_r",
        scope: "session", saved_as: "personal" }], overrides: [] } };
    }
    if (m === "DELETE" && p === "/api/v2/memory/corrections/corr_r") return { status: 200, body: { id: "corr_r", status: "revoked" } };
    return null;
  });
  renderAt(`/w/${WS}/ask?thread=${THREAD_OLD}`);
  const chip = within(await screen.findByLabelText("Meanings the next answer will use")).getByRole("listitem");
  expect(chip.className).toContain("memory-personal");
  expect(chip.querySelector(".memory-scope")?.textContent).toBe("Personal");
  fireEvent.click(within(chip).getByRole("button", { name: "Forget" }));
  await waitFor(() => expect(calls(fetchMock, "DELETE", /\/memory\/corrections\/corr_r$/)).toHaveLength(1));
});
