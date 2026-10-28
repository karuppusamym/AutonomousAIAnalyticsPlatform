import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { BuilderChatPanel, describeOp, diffDocuments } from "../components/DashboardBuilder";
import type { BuilderDashboard, BuilderDocument, BuilderTurn } from "../api";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const doc: BuilderDocument = {
  id: "ops", title: "Ops", layout: { items: [{ ref: "trend", x: 0, y: 0, w: 6, h: 4 }] },
  charts: [{ id: "trend", title: "Trend", type: "line", fields: [{ name: "week", type: "temporal" }, { name: "n", type: "quantitative" }],
    dataset_ref: { kind: "verified_query", name: "weekly", version: 1 }, encoding: { x: "week", y: ["n"] } }],
  kpis: [], text_blocks: [], filters: [],
};
const dash = (version: number, d = doc): BuilderDashboard => ({
  id: "art_1", workspace_id: "ws", name: "dashboard:ops", version, status: "draft", platform: null, external_url: null,
  content_hash: `h${version}`, updated_at: null, document: d,
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("builder helpers", () => {
  it("describes ops in plain words", () => {
    expect(describeOp({ op: "chart.set_type", chart_id: "trend", type: "bar" })).toBe("trend → bar chart");
    expect(describeOp({ op: "query.extend_period", chart_ids: ["trend"], compare: "yoy" })).toBe("compare YOY on trend");
    expect(describeOp({ op: "layout.move", ref: "kpi", x: 0, y: 2 })).toBe("move kpi to row 3, column 1");
  });

  it("diffs two versions element by element", () => {
    const after: BuilderDocument = { ...doc, charts: [{ ...doc.charts[0], type: "bar" }],
      layout: { items: [{ ref: "trend", x: 6, y: 0, w: 6, h: 4 }] },
      filters: [{ id: "f_region", field: "region", op: "eq", value: "EMEA" }] };
    expect(diffDocuments(doc, after)).toEqual(["changed chart trend", "added filter f_region", "moved trend"]);
  });
});

describe("BuilderChatPanel", () => {
  it("sends the request with the version it was based on and shows what was applied", async () => {
    const turn: BuilderTurn = { status: "applied", dashboard: dash(2), refused: [], deferred: [],
      proposal: { ops: [{ op: "chart.set_type", chart_id: "trend", type: "area" }], answered_by: "rules", notes: [], model: null } };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse(turn));
    const onUpdated = vi.fn();
    render(<BuilderChatPanel dashboard={dash(1)} focus="trend" onUpdated={onUpdated} />);
    fireEvent.change(screen.getByLabelText("Describe a change"), { target: { value: "make it an area chart" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(screen.getByText("Applied: now version 2.")).toBeTruthy());
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("/api/v2/dashboards/art_1/messages");
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({ text: "make it an area chart", focus: "trend", base_version: 1 });
    expect(screen.getByText("trend → area chart")).toBeTruthy();
    expect(onUpdated).toHaveBeenCalledWith(turn.dashboard, turn);
  });

  it("shows a refusal with its reason and applies the suggested op on request", async () => {
    const refused: BuilderTurn = { status: "refused", dashboard: dash(1), deferred: [],
      refused: [{ index: 0, op: "chart.set_type", reason: "a line chart needs a temporal x field",
        suggestion: { op: "chart.set_type", chart_id: "trend", type: "bar" } }],
      proposal: { ops: [{ op: "chart.set_type", chart_id: "trend", type: "line" }], answered_by: "rules", notes: [], model: null } };
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse(refused))
      .mockResolvedValueOnce(jsonResponse({ status: "applied", dashboard: dash(2), refused: [], deferred: [] }));
    render(<BuilderChatPanel dashboard={dash(1)} focus={null} onUpdated={() => undefined} />);
    fireEvent.change(screen.getByLabelText("Describe a change"), { target: { value: "make trend a line" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(screen.getByText(/a line chart needs a temporal x field/)).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Apply instead: trend → bar chart" }));
    await waitFor(() => expect(screen.getByText("Applied: now version 2.")).toBeTruthy());
    expect(String(fetchMock.mock.calls[1][0])).toBe("/api/v2/dashboards/art_1/ops");
    expect(JSON.parse(String((fetchMock.mock.calls[1][1] as RequestInit).body)).ops).toEqual([refused.refused![0].suggestion]);
  });

  it("says when an edit of a published dashboard went to a draft", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ status: "applied", dashboard: { ...dash(2), id: "art_2" },
      forked_from: "art_1", refused: [], deferred: [] }));
    render(<BuilderChatPanel dashboard={{ ...dash(3), status: "published" }} focus={null} onUpdated={() => undefined} />);
    fireEvent.change(screen.getByLabelText("Describe a change"), { target: { value: "remove trend" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(screen.getByText(/went to a draft copy/)).toBeTruthy());
  });
});
