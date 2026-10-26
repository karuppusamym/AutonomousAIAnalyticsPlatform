# Sample-record follow-ups — 2026-10-25

## Problem and change

The retail conversation `ask_1dd26dcc7c4a` answered “How many orders are there?” but refused “list down few same order” as an ambiguous duplicate-order request. Ask persisted thread history but did not expose the preceding answer to the SQL path for this follow-up.

The service now supplies the most recent successful answer's SQL and rule metadata inside the authorized thread. `agents/ask_samples.py` handles a strict bounded grammar for example-record requests before model generation. It resolves the table against current scoped catalog metadata, preserves readable filters from a prior single-table answer, builds SQL with SQLGlot, and executes only through QueryGateway after Ask authorization and budget checks. It does not send prior rows to a model.

Requests support 1–20 example rows (five for “few”). Personal fields are excluded. “Few same” is treated as a typo for samples only with a matching prior table, with an explicit interpretation in the answer. Explicit duplicate requests, new qualified filters, ambiguous tables, unsupported requests and unreadable inherited filters fall through to existing handling rather than having qualifiers silently discarded.

Related details use existing validated same-source many-to-one/one-to-one relationships. The base population is limited before joins. Prior sample filters survive the related-detail follow-up. No arbitrary joins, operational changes or access expansion were added. Sample rows are labelled ordered examples, not representative statistical samples. This is bounded follow-up support, not general conversational memory for every SQL question.

## Validation

- 126 relevant unit tests passed: sample planning, Ask rules, placeholder refusal and thread behavior (`var/ask-sample-tests-final.log`). Includes inherited filters, explicit new tables, personal fields, unsupported requests, missing relationships, revoked filter-column access and preservation across sample/detail follow-up.
- One live-Postgres API integration test passed (`test_example_followup_uses_prior_filtered_population_without_a_model`, `var/ask-sample-integration.log`): priority-filtered incident count followed by five sample incidents, matching parent turn, retained predicate and no model call. The prior count SQL uses fake model transport; the sample query reads actual staged test data through the gateway.
- Real local retail conversation: exact wording “list down few same order” produced answered rule turn `askt_f9badf152fe5` with five rows. “Show 5 sample orders with related details” produced `askt_54191e4967f7`, five rows referencing orders, customers and products. The same five order IDs are retained. Both responses have no refusal and use rules rather than a model (`var/ask-sample-live.json`, `var/ask-sample-live.log`; ignored local evidence).
- One real-API browser journey passed after reload: five data rows visible, related-details explanation and inspector row count correct (`web/e2e-live/09-sample-answer.live.spec.ts`, `var/ask-sample-browser.log`). Screenshot visually inspected. No browser route mocks or new approvals.
- Rebuilt local API, retained persistent runtime volume, healthy deployment. Running sample-module hash matches the working source. Targeted Ruff and whitespace checks pass.

Historical failed/refused turns remain unchanged as audit history; successful repaired turns were appended to the same conversation. Refresh the conversation to see them.
