import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

const embedDashboard = vi.fn();
vi.mock("@superset-ui/embedded-sdk", () => ({ embedDashboard }));

import { EmbeddedDashboard } from "../components/EmbeddedDashboard";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  embedDashboard.mockReset();
});

describe("EmbeddedDashboard", () => {
  it("embeds with the minted token first, and asks the platform again for every refresh", async () => {
    let minted = 0;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      minted += 1;
      return jsonResponse({ token: `guest-${minted}`, dashboard_uuid: "uuid-7", superset_url: "http://bi.test/", rls_clauses: 2 });
    });
    const unmount = vi.fn();
    embedDashboard.mockResolvedValue({ unmount, getScrollSize: async () => ({ width: 800, height: 900 }) });

    const view = render(<EmbeddedDashboard artifactId="art_1" title="Ops" />);
    await waitFor(() => expect(embedDashboard).toHaveBeenCalledTimes(1));
    const params = embedDashboard.mock.calls[0][0];
    expect(params.id).toBe("uuid-7");
    expect(params.supersetDomain).toBe("http://bi.test");
    expect(params.mountPoint).toBe(screen.getByTestId("superset-embed"));
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/v2/dashboards/art_1/embed-token");
    expect(await params.fetchGuestToken()).toBe("guest-1");  // no second mint for the first load
    expect(await params.fetchGuestToken()).toBe("guest-2");  // a refresh re-checks access server side
    expect(screen.getByText(/2 row filters from your workspace policy applied/)).toBeTruthy();
    view.unmount();
    expect(unmount).toHaveBeenCalled();
  });

  it("shows the platform's refusal and never loads Superset", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ error: { code: "forbidden", message: "this dashboard was not published for your audience" } }, 403));
    render(<EmbeddedDashboard artifactId="art_1" title="Ops" />);
    expect(await screen.findByText(/not published for your audience/)).toBeTruthy();
    expect(embedDashboard).not.toHaveBeenCalled();
  });
});
