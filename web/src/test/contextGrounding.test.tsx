import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { session } from "../api";
import { bodyOf, calls, mockFetch, renderAt } from "./harness";
import { resetMockState, USER, WS } from "./mockBackend";

beforeEach(()=>{resetMockState();session.set("mock-token",USER);});
afterEach(()=>{cleanup();vi.restoreAllMocks();});

it("maps a reviewed term without metadata JSON and resolves the question from it",async()=>{
  const fetch=mockFetch();renderAt(`/w/${WS}/data/catalog?tab=grounding`);
  fireEvent.click(await screen.findByRole("button",{name:"Map a business term"}));
  const form=await screen.findByRole("region",{name:"Guided domain onboarding"});
  fireEvent.change(within(form).getByLabelText("Business term"),{target:{value:"Repeat"}});
  fireEvent.change(within(form).getByLabelText("Business definition"),{target:{value:"Repeat flag follows the reviewed incident policy"}});
  fireEvent.click(within(form).getByLabelText(/s.incident.repeat_flag/));
  fireEvent.change(within(form).getByLabelText("Review owner"),{target:{value:USER.id}});
  fireEvent.change(within(form).getByLabelText("Supporting evidence or policy reference"),{target:{value:"Service policy v1"}});
  fireEvent.change(within(form).getByLabelText("Validation examples and expected interpretation"),{target:{value:"Repeat true; first occurrence false"}});
  fireEvent.change(within(form).getByLabelText("Review state"),{target:{value:"approved"}});
  fireEvent.click(within(form).getByRole("button",{name:"Save mapping"}));
  await screen.findByText("Mapping saved with its owner, applicability and revision.");
  expect(bodyOf(calls(fetch,"POST",/\/grounding\/bindings$/)[0][1])).toMatchObject({columns:["s.incident.repeat_flag"],state:"approved",owner_id:USER.id});
  fireEvent.change(screen.getByLabelText("Business question"),{target:{value:"Why did repeat incidents increase?"}});
  fireEvent.click(screen.getByRole("button",{name:"Resolve meaning"}));
  await screen.findByText(/No unresolved business definitions/);
  fireEvent.click(screen.getByRole("button",{name:"Check mapped data for Repeat"}));
  await screen.findByText(/98 non-null/);
});

it("shows unresolved meaning after context assistance instead of claiming certainty",async()=>{
  mockFetch();renderAt(`/w/${WS}/data/catalog?tab=grounding`);
  fireEvent.change(await screen.findByLabelText("Business question"),{target:{value:"Why did repeat incidents increase?"}});
  fireEvent.click(screen.getByRole("button",{name:"Resolve meaning"}));
  expect(await screen.findByText("Define repeat for this population")).toBeTruthy();
});

it("records and evaluates golden grounding questions separately from real pilot scores",async()=>{
  const fetch=mockFetch();renderAt(`/w/${WS}/data/catalog?tab=grounding`);
  fireEvent.click(await screen.findByText("Add a golden question"));
  fireEvent.change(screen.getByLabelText("Golden question"),{target:{value:"Explain unresolved repeat incidents"}});
  fireEvent.click(screen.getByLabelText("Expect a clarification instead of a guessed definition"));
  fireEvent.click(screen.getByRole("button",{name:"Save golden question"}));
  await screen.findByText(/Explain unresolved repeat incidents · Clarification expected/);
  fireEvent.click(screen.getByRole("button",{name:"Evaluate grounding"}));
  await screen.findByText(/1 \/ 1 grounding checks passed/);
  expect(bodyOf(calls(fetch,"POST",/\/golden\/evaluate$/)[0][1])).toEqual({use_model:false});
});
