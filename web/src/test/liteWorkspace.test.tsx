import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { session } from "../api";
import { to } from "../routes";
import { mockFetch, renderAt } from "./harness";
import { resetMockState, USER, WS } from "./mockBackend";

beforeEach(() => { sessionStorage.clear(); resetMockState(); session.set("mock-token", USER); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); session.clear(); sessionStorage.clear(); });

it("opens Lite, navigates real workspace tools, and returns to the full interface", async () => {
  mockFetch();
  renderAt(to.lite(WS));
  const nav = await screen.findByRole("navigation", { name: "Lite workspace" });
  expect(await screen.findByLabelText("Question")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "History" }));
  expect(screen.getByRole("button", { name: "History" }).getAttribute("aria-expanded")).toBe("true");
  expect(screen.getByRole("complementary", { name: "Workflow guide" })).toBeTruthy();
  fireEvent.click(within(nav).getByRole("link", { name: "Data" }));
  expect(await screen.findByRole("heading", { name: "Data sources", level: 1 })).toBeTruthy();
  expect(screen.getByRole("navigation", { name: "Lite workspace" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Full interface" }));
  expect(await screen.findByRole("navigation", { name: "Main" })).toBeTruthy();
  expect(sessionStorage.getItem("analystos.ui")).toBeNull();
});

it("resizes both side panels with bounds and converts to focus mode and back", async () => {
  mockFetch(); renderAt(to.lite(WS));
  const left = await screen.findByRole("separator", { name: "Resize navigation panel" });
  const right = screen.getByRole("separator", { name: "Resize workflow panel" });
  fireEvent.keyDown(left, { key: "ArrowRight" });
  expect(left.getAttribute("aria-valuenow")).toBe("240");
  fireEvent.keyDown(right, { key: "ArrowLeft" });
  expect(right.getAttribute("aria-valuenow")).toBe("320");
  fireEvent.keyDown(left, { key: "Home" });
  fireEvent.keyDown(left, { key: "ArrowLeft" });
  expect(left.getAttribute("aria-valuenow")).toBe("160");
  fireEvent.keyDown(right, { key: "End" });
  expect(right.getAttribute("aria-valuenow")).toBe("480");
  fireEvent.doubleClick(right);
  expect(right.getAttribute("aria-valuenow")).toBe("300");
  const question = await screen.findByLabelText("Question");
  fireEvent.change(question, { target: { value: "Keep this draft" } });
  fireEvent.click(screen.getByRole("button", { name: "Focus mode" }));
  expect(screen.queryByRole("navigation", { name: "Lite workspace" })).toBeNull();
  expect(screen.queryByRole("complementary", { name: "Workflow guide" })).toBeNull();
  expect((screen.getByLabelText("Question") as HTMLTextAreaElement).value).toBe("Keep this draft");
  fireEvent.click(screen.getByRole("button", { name: "Three panels" }));
  expect(screen.getAllByRole("separator")).toHaveLength(2);
  expect(screen.getByRole("separator", { name: "Resize navigation panel" }).getAttribute("aria-valuenow")).toBe("160");
});

it("requires authentication for the Lite entry point", async () => {
  session.clear(); mockFetch(); renderAt(to.lite(WS));
  expect(await screen.findByRole("heading", { name: "Sign in" })).toBeTruthy();
  expect(screen.queryByRole("navigation", { name: "Lite workspace" })).toBeNull();
});
