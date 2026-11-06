import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

const sources = [
  { id: "ks_pending", kind: "github", name: "Docs repo", config: { repo: "acme/docs", branch: "main", path: "docs" },
    secret_ref: "env:ANALYSTOS_SOURCE_SECRET_GH", enabled: true, tracked_files: 0, last_sync_at: null, last_status: null,
    last_report: {}, binding_approval_id: "apr_1", binding_status: "pending" },
  { id: "ks_ok", kind: "confluence", name: "Wiki", config: { base_url: "https://wiki.acme.test", space: "OPS" },
    secret_ref: "env:ANALYSTOS_SOURCE_SECRET_WIKI", enabled: true, tracked_files: 3, last_sync_at: null, last_status: null,
    last_report: {}, binding_approval_id: "apr_2", binding_status: "approved" },
];

vi.mock("../api", async (orig) => ({ ...(await orig<typeof import("../api")>()), request: vi.fn(async () => sources) }));

import { KnowledgeSources } from "../components/KnowledgeSources";

afterEach(cleanup);

describe("Knowledge sources: binding approval", () => {
  it("shows the approval state and syncs only an approved source", async () => {
    render(<KnowledgeSources wsId="ws_1" canEdit />);
    expect(await screen.findByText("awaiting approval")).toBeTruthy();
    expect(screen.getByText("approved")).toBeTruthy();
    expect(screen.getByRole("link", { name: "approvals inbox" }).getAttribute("href")).toBe("/w/ws_1/operate/approvals");
    const [pending, approved] = screen.getAllByRole("button", { name: "Sync now" }) as HTMLButtonElement[];
    expect(pending.disabled).toBe(true);
    expect(pending.title).toMatch(/Waiting for the approval/);
    expect(approved.disabled).toBe(false);
  });
});
