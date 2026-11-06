import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { session } from "../api";
import { ContextWebhooks } from "../components/ContextWebhooks";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const USER = { id: "u1", email: "a@b", name: "A", is_admin: false, active: true, attributes: {}, created_at: "" };
const HOOK = {
  id: "cwh_1", workspace_id: "ws_1", name: "schema alerts", kind: "webhook", url: "https://hooks.example.com/c", has_secret: true,
  filters: { kinds: ["dataset"], changes: [], tag_added: [], schema_changed: true }, status: "authorized", authorized: true,
  authorized_until: "2026-12-27T00:00:00Z", approval_id: "apr_1", destination_hash: "h", cursor: 4, revision: 1,
  summary: "POST ledger change metadata to https://hooks.example.com/c when kinds dataset; schema changed",
};
const DEAD = {
  id: "cwd_1", webhook_id: "cwh_1", seq: 7, entry_id: "cle_7", status: "dead_letter", attempts: 3, max_attempts: 3,
  last_error: "hooks.example.com answered 503", next_attempt_at: null, delivered_at: null, created_at: "2026-11-05T10:00:00Z",
};

beforeEach(() => session.set("tok123", USER));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

describe("context change push subscriptions (P1-6)", () => {
  it("lists subscriptions and redrives a dead letter", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
      const u = String(url);
      if (u.endsWith("/redrive") && init?.method === "POST") return jsonResponse({ ...DEAD, status: "queued", attempts: 0 });
      if (u.includes("/api/v2/context/webhook-deliveries")) return jsonResponse([DEAD]);
      if (u.includes("/api/v2/context/webhooks")) return jsonResponse([HOOK]);
      return jsonResponse([]);
    });
    render(<ContextWebhooks wsId="ws_1" canEdit />);
    expect(await screen.findByText(HOOK.summary)).toBeTruthy();
    expect(screen.getByText("hooks.example.com answered 503")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([u, i]) => String(u).endsWith("/webhook-deliveries/cwd_1/redrive")
      && (i as RequestInit | undefined)?.method === "POST")).toBe(true));
  });

  it("registers a webhook and says it awaits approval", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
      if (String(url).endsWith("/api/v2/context/webhooks") && init?.method === "POST") {
        return jsonResponse({ ...HOOK, status: "pending", authorized: false, approval_id: "apr_9" }, 201);
      }
      return jsonResponse([]);
    });
    render(<ContextWebhooks wsId="ws_1" canEdit />);
    fireEvent.click(await screen.findByRole("button", { name: "Add webhook" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "schema alerts" } });
    fireEvent.change(screen.getByLabelText("Webhook URL"), { target: { value: "https://hooks.example.com/c" } });
    fireEvent.click(screen.getByRole("button", { name: "Request authorization" }));
    expect(await screen.findByText(/sends nothing until an approver/)).toBeTruthy();
  });
});
