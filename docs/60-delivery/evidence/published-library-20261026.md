# Published workspace collection — 2026-10-26

## Delivered

`web/src/components/PublishedOutputs.tsx` appears on Outputs (All, Dashboards and Reports).
It uses the existing workspace-authorized artifact list and additionally filters by workspace ID,
published status and dashboard/report type. It spans investigations intentionally, independently of
the investigation picker. One BI object is shown once, using the newest publication record by
updated time, keyed by platform/type/external ID; distinct BI objects with the same title remain.

Search uses business titles and BI platform names. Direct BI links open the configured public URL
with no credentials or tokens appended. Only HTTP(S) URLs without embedded credentials become links
in this collection. Details & evidence clears the general output search and selects the output's
type, artifact and originating investigation. Draft publication still uses the existing approval flow.
Separate BI sign-in and the fact that publication does not refresh analysis are explained in place.

## Validation

- Nine tests passed across `publishedOutputs.test.tsx` and `outputNames.test.ts`: workspace filtering,
  publication status/type selection, cross-investigation collection, newest external-object record,
  evidence action, search/empty states, invalid links and empty collection.
- `npm run build` passed; existing bundle-size advisory remains.
- Live UI at `127.0.0.1:5173`: signed into the real API, opened Retail Outputs, observed four
  published dashboards (16, 17, 28, 29), searched Delivery, and clicked Details & evidence.
  Resulting selection was `art_772bffe0642a` in `run_7896fbdad581` with dashboard preview and lineage.
  Screenshot saved locally to `var/published-library-browser.jpg` (ignored runtime evidence).

## Limits

This adds navigation over saved publication records. It does not probe BI availability, refresh data,
validate an old publication's current contents, grant BI access or implement SSO. Superset still
uses its own login session. Independent selected-collection publication remains BI-VIEWS-02.
