import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { session } from "../api";
import { bodyOf, calls, mockFetch, renderAt } from "./harness";
import { resetMockState, RUN, USER, WS } from "./mockBackend";

beforeEach(() => { resetMockState(); session.set("mock-token", USER); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); session.clear(); });

it("drafts a suggested question without executing it, then displays its saved-evidence answer", async () => {
  const fetch = mockFetch();
  renderAt(`/w/${WS}/work/investigations/${RUN}`);
  const panel = await screen.findByRole("region", { name: "Investigation conversation" });
  fireEvent.click(within(panel).getByRole("button", { name: "Summarize the findings" }));
  expect(calls(fetch, "POST", /\/feedback$/)).toHaveLength(0);
  expect((within(panel).getByLabelText("Message") as HTMLTextAreaElement).value).toBe("Summarize the findings");
  fireEvent.click(within(panel).getByRole("button", { name: "Send" }));
  expect(await within(panel).findByText(/One finding is currently verified/)).toBeTruthy();
  expect(bodyOf(calls(fetch, "POST", /\/feedback$/)[0][1])).toMatchObject({ kind: "question", text: "Summarize the findings" });
  expect(within(panel).getByRole("link", { name: /F1 · Network/ }).getAttribute("href")).toContain("ins_demo");
  expect(within(panel).getByText(/no new analysis/)).toBeTruthy();
});

it("keeps the conversation when the investigation page is remounted", async () => {
  mockFetch();
  const view = renderAt(`/w/${WS}/work/investigations/${RUN}`);
  fireEvent.change(await screen.findByLabelText("Message"), { target: { value: "Summarize the findings" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  expect(await screen.findByText(/One finding is currently verified/)).toBeTruthy();
  view.unmount();
  renderAt(`/w/${WS}/work/investigations/${RUN}`);
  expect(await screen.findByText(/One finding is currently verified/)).toBeTruthy();
});

it("retains the question and shows the error when saving fails", async () => {
  mockFetch((method, path) => method === "POST" && path.endsWith("/feedback")
    ? { status: 503, body: { error: { message: "Evidence is temporarily unavailable" } } } : null);
  renderAt(`/w/${WS}/work/investigations/${RUN}`);
  fireEvent.change(await screen.findByLabelText("Message"), { target: { value: "What are the limitations?" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  expect(await screen.findByText(/Evidence is temporarily unavailable/)).toBeTruthy();
  expect((screen.getByLabelText("Message") as HTMLTextAreaElement).value).toBe("What are the limitations?");
});
