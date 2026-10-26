import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { Artifact } from "../api";
import { PublishedOutputs } from "../components/PublishedOutputs";

afterEach(cleanup);
function artifact(patch: Partial<Artifact> = {}): Artifact {
  return {
    id: "art_delivery", workspace_id: "retail", run_id: "run_current", type: "dashboard",
    name: "topic_delivery", version: 1, status: "published", platform: "superset",
    external_id: "29", external_url: "http://localhost:8088/superset/dashboard/29/",
    creator_agent: "visualization", creator_user: null, content: { title: "Retail / Delivery reliability" },
    content_hash: "hash", created_at: "2026-10-25T12:00:00Z", updated_at: "2026-10-25T12:00:00Z", ...patch,
  };
}

it("shows only this workspace's published reports and dashboards, across investigations", () => {
  render(<PublishedOutputs wsId="retail" artifacts={[
    artifact(), artifact({ id: "report", type: "report", run_id: "run_earlier", external_id: "77", content: { title: "Executive report" } }),
    artifact({ id: "other", workspace_id: "operations", content: { title: "Private operations dashboard" } }),
    artifact({ id: "draft", status: "draft", content: { title: "Unapproved draft" } }),
    artifact({ id: "chart", type: "chart", name: "Individual chart" }),
  ]} onSelect={vi.fn()} />);
  expect(screen.getByText("Published dashboards & reports (2)")).toBeTruthy();
  expect(screen.getByText("Executive report")).toBeTruthy();
  expect(screen.queryByText("Private operations dashboard")).toBeNull();
  expect(screen.queryByText("Unapproved draft")).toBeNull();
  expect(screen.queryByText("Individual chart")).toBeNull();
  expect(screen.getByRole("link", { name: /Open Retail/ }).getAttribute("href")).toContain("/dashboard/29/");
});

it("uses the latest publication record for a shared BI object and opens its evidence", () => {
  const select = vi.fn();
  const latest = artifact({ id: "latest", updated_at: "2026-10-26T12:00:00Z", content: { title: "Current delivery dashboard" } });
  render(<PublishedOutputs wsId="retail" artifacts={[artifact(), latest]} onSelect={select} />);
  expect(screen.getAllByRole("link")).toHaveLength(1);
  expect(screen.queryByText("Retail / Delivery reliability")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /View details/ }));
  expect(select).toHaveBeenCalledWith(latest);
});

it("searches business titles and explains empty searches", () => {
  render(<PublishedOutputs wsId="retail" artifacts={[artifact()]} onSelect={vi.fn()} />);
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "  DELIVERY " } });
  expect(screen.getByRole("link")).toBeTruthy();
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "missing" } });
  expect(screen.queryByRole("link")).toBeNull();
  expect(screen.getByText("No published items match your search.")).toBeTruthy();
});

it.each([null, "javascript:alert(1)", "https://admin:password@example.test/dashboard/29/"])("keeps evidence accessible without exposing an invalid published link: %s", (url) => {
  render(<PublishedOutputs wsId="retail" artifacts={[artifact({ external_url: url })]} onSelect={vi.fn()} />);
  expect(screen.queryByRole("link")).toBeNull();
  expect(screen.getByRole("button", { name: /View details/ })).toBeTruthy();
});

it("does not show an empty collection for workspaces with drafts only", () => {
  const { container } = render(<PublishedOutputs wsId="retail" artifacts={[artifact({ status: "draft" })]} onSelect={vi.fn()} />);
  expect(container.textContent).toBe("");
});
