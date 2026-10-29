import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { DashboardSummary } from "../components/DashboardSummary";
import { describeReportOp, ReportBuilder, ReportChatPanel, ReportOutline } from "../components/ReportBuilder";
import type { BuilderDashboard, DashboardStory, ReportDocTurn, ReportDocView } from "../api";
import { setExpertView } from "../lib/expert";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const report = (version: number, extra: Partial<ReportDocView["document"]> = {}): ReportDocView => ({
  id: "art_r", workspace_id: "ws", name: "report:weekly", version, status: "draft", content_hash: `h${version}`, updated_at: null,
  document: {
    id: "weekly", title: "Weekly operations", sections: [
      { id: "summary", title: "Summary", blocks: [{ kind: "text", id: "t_summary", markdown: "Open incidents are 42." },
        { kind: "kpi", id: "kpi_open", title: "Open incidents", value: 42, previous_value: 38, source: { label: "Dashboard “Ops” v2, KPI “Open incidents”" }, ref: {} }] },
      { id: "emea_backlog", title: "EMEA backlog", blocks: [{ kind: "chart", id: "chart_backlog", title: "EMEA backlog by priority",
        chart_type: "bar", columns: ["priority", "open"], rows: [["P1", 7], ["P2", 31]], source: { label: "Dashboard “Ops” v2, chart “EMEA backlog by priority”" }, ref: {} }] },
    ], ...extra,
  },
});

beforeEach(() => setExpertView(false));  // business view, as people open the app

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("report helpers", () => {
  it("describes report ops in plain words", () => {
    expect(describeReportOp({ op: "section.add", title: "EMEA backlog" })).toBe("add section “EMEA backlog”");
    expect(describeReportOp({ op: "block.add", section_id: "risks", ref: { kind: "dashboard_chart" } })).toBe("add dashboard chart to risks");
    expect(describeReportOp({ op: "export.request", format: "docx" })).toBe("ask to export as Word");
    expect(describeReportOp({ op: "section.move", section_id: "risks", direction: "up" })).toBe("move section risks up");
  });

  it("lists sections and blocks in business words; ids only with expert tools", () => {
    const { rerender } = render(<ReportOutline report={report(3)} focus={null} onFocus={() => undefined} />);
    expect(screen.getByText("EMEA backlog")).toBeTruthy();
    expect(screen.getByText("Open incidents are 42.")).toBeTruthy();
    expect(screen.getByText(/Chart · from Dashboard “Ops” v2/)).toBeTruthy();
    expect(screen.queryByText("chart_backlog")).toBeNull();
    setExpertView(true);
    rerender(<ReportOutline report={report(3)} focus={null} onFocus={() => undefined} />);
    expect(screen.getByText("chart_backlog")).toBeTruthy();
  });

  it("shows held text as a warning, never the stale numbers", () => {
    const held = report(4);
    held.document.sections[0].blocks[0] = { kind: "text", id: "t_summary", markdown: "Open incidents are 42.", held: "This paragraph quoted numbers that changed." };
    render(<ReportOutline report={held} focus={null} onFocus={() => undefined} />);
    expect(screen.getByText("This paragraph quoted numbers that changed.")).toBeTruthy();
    expect(screen.queryByText("Open incidents are 42.")).toBeNull();
  });
});

describe("ReportChatPanel", () => {
  it("sends the request with its base version and reports the outcome", async () => {
    const turn: ReportDocTurn = { status: "applied", report: report(2), refused: [], deferred: [],
      proposal: { ops: [{ op: "section.add", title: "EMEA backlog" }], answered_by: "rules", notes: [], model: null } };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse(turn));
    const onUpdated = vi.fn();
    render(<ReportChatPanel report={report(1)} focus="summary" onUpdated={onUpdated} />);
    fireEvent.change(screen.getByLabelText("Describe a change to the report"), { target: { value: "add a section on EMEA backlog" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(screen.getByText("Done: the report is now version 2.")).toBeTruthy());
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("/api/v2/report-documents/art_r/messages");
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({ text: "add a section on EMEA backlog", focus: "summary", base_version: 1 });
    expect(onUpdated).toHaveBeenCalledWith(turn.report, turn);
  });

  it("shows a refused number with its reason", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ status: "refused", report: report(1), deferred: [],
      refused: [{ index: 0, op: "text.set", reason: "the text quotes numbers this report cannot back: 99" }],
      proposal: { ops: [{ op: "text.set" }], answered_by: "rules", notes: [], model: null } }));
    render(<ReportChatPanel report={report(1)} focus={null} onUpdated={() => undefined} />);
    fireEvent.change(screen.getByLabelText("Describe a change to the report"), { target: { value: "set the summary to: 99 closed" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(screen.getByText(/cannot back: 99/)).toBeTruthy());
  });
});

describe("ReportBuilder export", () => {
  it("asks for an approval, then downloads with it", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/versions")) return jsonResponse([{ version: 1, diff: [{ op: "create" }], content_hash: "h1", created_by: "user:u", created_at: null }]);
      if (url.endsWith("/exports") && (init as RequestInit)?.method === "POST") {
        return jsonResponse({ status: "approval_required", format: "pdf", snapshot_hash: "abc", sensitivity: "internal",
          approval: { id: "apr_1", status: "pending", risk_tier: "medium" } }, 202);
      }
      if (url.includes("/exports/pdf")) return new Response(new Blob(["%PDF"]), { status: 200, headers: { "Content-Type": "application/pdf" } });
      return jsonResponse(report(1));
    });
    const saved = vi.fn();
    vi.spyOn(URL, "createObjectURL").mockImplementation(() => { saved(); return "blob:x"; });
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    render(<ReportBuilder docId="art_r" role="editor" />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Ask to export" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Ask to export" }));
    await waitFor(() => expect(screen.getByText("apr_1")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Download PDF" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([u]) => String(u).includes("/api/v2/report-documents/art_r/exports/pdf?approval_id=apr_1"))).toBe(true));
  });

  it("does not offer export to a viewer", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
      String(input).endsWith("/versions") ? jsonResponse([]) : jsonResponse(report(1)));
    render(<ReportBuilder docId="art_r" role="viewer" />);
    await waitFor(() => expect(screen.getByText("Editors can ask to export this report.")).toBeTruthy());
    expect(screen.queryByRole("button", { name: "Ask to export" })).toBeNull();
  });
});

describe("DashboardSummary", () => {
  const dash = { id: "art_d", workspace_id: "ws", name: "dashboard:ops", version: 4, status: "draft", platform: null,
    external_url: null, content_hash: "h", updated_at: null,
    document: { id: "ops", title: "Ops", layout: { items: [] }, charts: [], kpis: [], text_blocks: [], filters: [] } } as BuilderDashboard;
  const story: DashboardStory = { dashboard_id: "art_d", version: 4, previous_version: 3, title: "Ops", shows: "Ops shows 2 charts.",
    facts: [{ kind: "leader", text: "EMEA leads Backlog by region with 75 backlog.", score: 1, chart_id: "c", values: [75], how: "" }],
    changes: [{ kind: "kpi_change", text: "Open incidents is 42, up 4 from 38.", score: 1, chart_id: null, values: [42, 38, 4], how: "" }],
    wording: "rules", notes: [], story_hash: "abcdef123456789", markdown: "" };

  it("shows what the dashboard says and what changed, and turns it into a report", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) =>
      String(input).endsWith("/story/report") ? jsonResponse(report(1), 201) : jsonResponse(story));
    render(<MemoryRouter><DashboardSummary dashboard={dash} /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("EMEA leads Backlog by region with 75 backlog.")).toBeTruthy());
    expect(screen.getByText("Open incidents is 42, up 4 from 38.")).toBeTruthy();
    expect(screen.queryByText(/abcdef123456/)).toBeNull();  // engineering detail only with expert tools
    fireEvent.click(screen.getByRole("button", { name: "Turn this into a report" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([u]) => String(u) === "/api/v2/dashboards/art_d/story/report")).toBe(true));
  });
});
