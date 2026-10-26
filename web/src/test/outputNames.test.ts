import { expect, it } from "vitest";
import { outputDisplayName } from "../lib/outputNames";

it("shows the business title for dashboards and reports", () => {
  expect(outputDisplayName({type:"dashboard",name:"topic_delivery",content:{title:"Retail / Delivery reliability"}})).toBe("Retail / Delivery reliability");
  expect(outputDisplayName({type:"report",name:"executive report",content:{title:"Operations executive report"}})).toBe("Operations executive report");
});

it("keeps existing names when a display title is missing or invalid", () => {
  expect(outputDisplayName({type:"dashboard",name:"executive",content:{title:" "}})).toBe("executive");
  expect(outputDisplayName({type:"report",name:"saved report",content:{title:12}})).toBe("saved report");
  expect(outputDisplayName({type:"dataset",name:"orders",content:{title:"unrelated metadata"}})).toBe("orders");
});
