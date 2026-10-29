import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { session } from "../api";
import { bodyOf, calls, mockFetch, renderAt } from "./harness";
import { resetMockState, USER, WS } from "./mockBackend";

beforeEach(()=>{resetMockState();session.set("mock-token",USER);window.localStorage.clear();});
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

it("discards a previous meaning result when the question or model choice changes",async()=>{
  mockFetch();renderAt(`/w/${WS}/data/catalog?tab=grounding`);
  fireEvent.change(await screen.findByLabelText("Business question"),{target:{value:"Why did repeat incidents increase?"}});
  fireEvent.click(screen.getByRole("button",{name:"Resolve meaning"}));
  await screen.findByText("Define repeat for this population",{},{timeout:5000});
  fireEvent.change(screen.getByLabelText("Business question"),{target:{value:"Compare incident priorities this month"}});
  expect(screen.queryByText("Define repeat for this population")).toBeNull();
  fireEvent.click(screen.getByRole("button",{name:"Resolve meaning"}));
  await screen.findByText("Define repeat for this population",{},{timeout:5000});
  fireEvent.click(screen.getByLabelText("Use model assistance after reading reviewed knowledge"));
  expect(screen.queryByText("Define repeat for this population")).toBeNull();
});

it("preserves boolean false in a reviewed population rule instead of sending text",async()=>{
  const fetch=mockFetch();renderAt(`/w/${WS}/data/catalog?tab=grounding`);
  fireEvent.click(await screen.findByRole("button",{name:"Map a business term"}));
  const form=await screen.findByRole("region",{name:"Guided domain onboarding"});
  fireEvent.change(within(form).getByLabelText("Business term"),{target:{value:"Non repeat"}});
  fireEvent.change(within(form).getByLabelText("Business definition"),{target:{value:"Only first occurrence incidents"}});
  fireEvent.click(within(form).getByLabelText(/s.incident.repeat_flag/));
  fireEvent.change(within(form).getByLabelText("Meaning type"),{target:{value:"population_rule"}});
  fireEvent.change(within(form).getByLabelText("Rule column"),{target:{value:"s.incident.repeat_flag"}});
  fireEvent.change(within(form).getByLabelText("Rule value type"),{target:{value:"boolean"}});
  fireEvent.change(within(form).getByLabelText("Rule value"),{target:{value:"false"}});
  fireEvent.click(within(form).getByRole("button",{name:"Save mapping"}));
  await screen.findByText("Mapping saved with its owner, applicability and revision.");
  expect(bodyOf(calls(fetch,"POST",/grounding\/bindings$/)[0][1])).toMatchObject({predicate:{column:"s.incident.repeat_flag",op:"=",value:false}});
});

it("opens the investigation preflight with the checked question and replaces an unrelated template draft",async()=>{
  mockFetch((method,path)=>method==="POST"&&path.endsWith("/grounding/resolve")?{status:200,body:{question:"Compare incident resolution times by team",resolutions:[],missing:[],needs_clarification:false,model_status:"used",used_count:0,available_count:0,clarification:[],metric_proposals:[]}}:null);
  window.localStorage.setItem(`analystos.startWork.${WS}`,JSON.stringify({templateId:"sla",sourceId:"old",objective:"Old objective"}));
  renderAt(`/w/${WS}/data/catalog?tab=grounding`);
  fireEvent.change(await screen.findByLabelText("Business question"),{target:{value:"Compare incident resolution times by team"}});
  fireEvent.click(screen.getByRole("button",{name:"Resolve meaning"}));
  fireEvent.click(await screen.findByRole("button",{name:"Investigate this question"}));
  const form=await screen.findByRole("form",{name:"Start explain"});
  expect(within(form).getByDisplayValue("Compare incident resolution times by team")).toBeTruthy();
  expect(within(form).queryByDisplayValue("Old objective")).toBeNull();
});

it("explains read-only access and prevents unavailable analysis actions for a viewer",async()=>{
  mockFetch((method,path)=>method==="GET"&&path===`/api/workspaces/${WS}`?{status:200,body:{id:WS,name:"Read only workspace",role:"viewer",members:[],policy:{}}}:null);
  renderAt(`/w/${WS}/data/catalog?tab=grounding`);
  await screen.findByText(/You can read definitions here/);
  expect((screen.getByRole("button",{name:"Resolve meaning"}) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.queryByRole("button",{name:"Map a business term"})).toBeNull();
});
