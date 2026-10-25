import { useId, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, request } from "../api";
import { to } from "../routes";
import { useAction, useAsync } from "../lib/hooks";
import { groundingApi, type Binding, type BindingInput, type GroundingReport, type KnownMeaning, type MetricMeaning, type Studio } from "../lib/grounding";
import { Card, ErrorBox, Field, Loading, Notice, Tag } from "./ui";

const empty = (): BindingInput => ({term: "", definition: "", kind: "definition", columns: [], domain: "", source_ids: [], owner_id: null, effective_from: null, expires_on: null, state: "draft", evidence: "", examples: "", metric: null, predicate: null});
const metricEmpty = (): MetricMeaning => ({outcome_column: "", numerator: "", denominator: "", grain: "", unit: "", time_column: null, exclusions: "", true_value: null});

export function ContextGrounding({wsId, canEdit}: {wsId: string; canEdit: boolean}) {
  const data = useAsync(() => groundingApi.studio(wsId), [wsId]);
  const members = useAsync(() => api.getWorkspace(wsId), [wsId]);
  const act = useAction();
  const [question, setQuestion] = useState("");
  const [model, setModel] = useState(true);
  const [report, setReport] = useState<GroundingReport>();
  const [edit, setEdit] = useState<Binding>();
  const [draft, setDraft] = useState<BindingInput>();
  const [notice, setNotice] = useState("");
  const [checks, setChecks] = useState<Awaited<ReturnType<typeof groundingApi.check>>>();
  const id = useId();
  const begin = (known?: KnownMeaning, binding?: Binding) => {
    setEdit(binding); setDraft(binding?.definition ?? {...empty(), term: known?.term ?? "", definition: known?.definition ?? "", columns: known?.columns ?? []});
  };
  const resolve = async (e: FormEvent) => {e.preventDefault(); const r = await act.run(() => groundingApi.resolve(wsId, question, model)); if (r) setReport(r);};
  const scan = async () => {
    const r = await act.run(() => request("POST", `/workspaces/${encodeURIComponent(wsId)}/knowledge/glossary/scan`, {use_model: model, include_ask: true, include_descriptions: true}));
    if (r) setNotice("Suggestions are ready in the review queue. Review them, then map the relevant definitions here.");
  };
  return <div className="stack grounding-studio">
    <Card title="Context and mapping coverage" label="Context mapping coverage">
      <p>Understand the question from reviewed knowledge and domain context first. Ask a person only when a material meaning remains unresolved.</p>
      <ErrorBox error={data.error} onRetry={data.reload} />
      {data.loading && <Loading />}
      {data.data && <><p><strong>{data.data.coverage.linked_columns} of {data.data.coverage.columns}</strong> available columns have knowledge mappings.</p>
        <p className="small muted">{data.data.coverage.note}</p>
        <details><summary>Columns without mapped knowledge</summary><ul className="list small">{data.data.coverage.unmapped_columns.map(c => <li key={c}>{c}</li>)}</ul></details>
        <p className="small">Applicable domains: {data.data.domains.join(", ") || "No matching domain pack; choose and review workspace definitions"}</p></>}
      {canEdit && <div className="btn-row"><button className="btn btn-primary" onClick={() => begin()}>Map a business term</button>
        <button className="btn" disabled={act.busy} onClick={() => void scan()}>Suggest business terms</button>
        <Link className="btn btn-ghost" to={to.data(wsId, "review")}>Review knowledge suggestions</Link></div>}
      {notice && <Notice tone="success">{notice}</Notice>}
    </Card>
    <Card title="Try a question before investigating" label="Resolve business meaning">
      <form className="form" onSubmit={resolve}><Field label="Business question" htmlFor={`${id}-question`}><textarea id={`${id}-question`} minLength={5} maxLength={4000} required value={question} onChange={e => setQuestion(e.target.value)} /></Field>
        <label className="toggle"><input type="checkbox" checked={model} onChange={e => setModel(e.target.checked)} /><span>Use model assistance after reading reviewed knowledge</span></label>
        <button className="btn btn-primary" disabled={act.busy}>Resolve meaning</button></form>
      {report && <><p className="small">{report.used_count} supporting entries resolved from {report.available_count} available entries · model: {report.model_status}</p>
        <Notice tone={report.needs_clarification ? "warning" : "success"}>{report.needs_clarification ? "Some meaning still needs review after context resolution." : "No unresolved business definitions were detected. Check the mappings before relying on the answer."}</Notice>
        <ul className="list">{report.resolutions.map((r, i) => <li key={`${r.id}-${i}`}><strong>{r.term}</strong> <Tag>{r.state}</Tag><p className="small">{r.definition}</p><p className="small mono">{r.columns.join(", ") || "No physical mapping"}</p><span className="small muted">{r.by} · {r.id}{r.revision ? ` · v${r.revision}` : ""}</span></li>)}</ul>
        {report.clarification.length > 0 && <ul className="list">{report.clarification.map(q => <li key={q}>{q}</li>)}</ul>}
        {!!report.analysis_filters?.length&&<details open><summary>Population and reporting constraints</summary><ul className="list small">{report.analysis_filters.map((f,i)=><li key={i}>{f.column} {f.op} {JSON.stringify(f.value)}</li>)}</ul></details>}
        {report.metric_proposals.map((p, i) => <div key={i} className="value-proposal"><strong>Proposed metric: {p.term}</strong><p>{p.definition}</p><p className="small">Missing contract: {p.missing_contract_fields.join(", ") || "Domain owner must validate the complete metric"}</p>{canEdit && <button className="btn btn-sm" onClick={() => {setEdit(undefined);setDraft({...empty(),kind:"metric",term:p.term,definition:p.definition,columns:p.columns,metric:metricEmpty()});}}>Review metric contract</button>}</div>)}</>}
    </Card>
    <ErrorBox error={act.error} />
    {draft && data.data && <MappingForm id={id} value={draft} setValue={setDraft} data={data.data} members={members.data?.members ?? []} busy={act.busy} cancel={() => setDraft(undefined)} save={async () => {
      const r = await act.run(() => groundingApi.save(wsId, draft, edit));
      if (r) {setDraft(undefined);setNotice("Mapping saved with its owner, applicability and revision.");await data.reload();}
    }} />}
    <Card title="Reviewed mappings and lifecycle" label="Reviewed mapping lifecycle">
      {data.data?.mappings.length === 0 && <p>No owner-managed mappings yet. Existing glossary knowledge remains available below.</p>}
      <ul className="list">{data.data?.mappings.map(b => <li key={b.id}><strong>{b.term}</strong> <Tag>{b.mapping_state}</Tag> <span className="small muted">v{b.revision} · {b.definition.kind}</span>
        <p>{b.definition.definition}</p><p className="small mono">{b.definition.columns.join(", ") || "Broad workspace guidance: no mapped columns"}</p>
        <p className="small">Owner: {members.data?.members.find(m => m.user_id === b.definition.owner_id)?.name || "Unassigned"} · Effective: {b.definition.effective_from || "Now"} · Expires: {b.definition.expires_on || "No expiry"}</p>
        {canEdit && <div className="btn-row"><button className="btn btn-sm" onClick={() => begin(undefined, b)}>Review or update {b.term}</button><button className="btn btn-sm" disabled={act.busy} onClick={async()=>{const r=await act.run(()=>groundingApi.check(wsId,b.id));if(r)setChecks(r);}}>Check mapped data for {b.term}</button></div>}</li>)}</ul>
      {checks&&<Notice tone="info"><p>{checks.note}</p><ul>{checks.checks.map(c=><li key={c.column}>{c.column}: {c.nonnull_rows} non-null / {c.sampled_rows} sampled rows; {c.distinct_values} distinct values{typeof c.matching_rows==="number"?`; ${c.matching_rows} match the rule`:""} · query {c.query_id}</li>)}</ul></Notice>}
    </Card>
    <Card title="Available knowledge is not the same as used knowledge">
      <p className="small muted">These entries are available to this scope. The question preview above and each run's context explanation show what was actually resolved.</p>
      <details><summary>Browse available definitions ({data.data?.available_knowledge.length ?? 0})</summary><ul className="list">{data.data?.available_knowledge.map(k => <li key={k.id}><strong>{k.term}</strong><p className="small">{k.definition}</p><p className="small mono">{k.columns.join(", ") || "No resolved column mapping"}</p>{canEdit && <button className="btn btn-sm" onClick={() => begin(k)}>Review mapping for {k.term}</button>}</li>)}</ul></details>
    </Card>
    <GoldenQuestions wsId={wsId} canEdit={canEdit} catalog={data.data?.catalog ?? []} />
  </div>;
}

function MappingForm({id,value,setValue,data,members,busy,save,cancel}: {id:string;value:BindingInput;setValue:(v:BindingInput)=>void;data:Studio;members:{user_id:string;name?:string|null}[];busy:boolean;save:()=>Promise<void>;cancel:()=>void}) {
  const set = (patch: Partial<BindingInput>) => setValue({...value,...patch});
  const metric = value.metric ?? metricEmpty();
  const [filter,setFilter] = useState("");
  const choices = (selected: string) => <><option value="">Choose a mapped column</option>{value.columns.map(c => <option key={c} value={c}>{c}{c===selected ? "" : ""}</option>)}</>;
  return <Card title="Guided domain mapping" label="Guided domain onboarding"><form className="form" onSubmit={e => {e.preventDefault();void save();}}>
    <p>1. Define the meaning. 2. Select data. 3. Add evidence and examples. 4. Review applicability and approval.</p>
    <Field label="Business term" htmlFor={`${id}-term`}><input id={`${id}-term`} required minLength={2} maxLength={200} value={value.term} onChange={e=>set({term:e.target.value})} /></Field>
    <Field label="Business definition" htmlFor={`${id}-definition`}><textarea id={`${id}-definition`} required minLength={5} maxLength={3000} value={value.definition} onChange={e=>set({definition:e.target.value})} /></Field>
    <Field label="Meaning type" htmlFor={`${id}-kind`}><select id={`${id}-kind`} value={value.kind} onChange={e=>set({kind:e.target.value as BindingInput["kind"],metric:e.target.value==="metric"?metric:null,predicate:["population_rule","quality_rule"].includes(e.target.value)?{column:"",op:"=",value:""}:null})}>
      <option value="definition">Business definition</option><option value="metric">Metric contract</option><option value="population_rule">Investigation population filter</option><option value="quality_rule">Data quality assertion</option><option value="policy">Policy guidance (not executable)</option></select></Field>
    <Field label="Find columns to map" htmlFor={`${id}-filter`}><input id={`${id}-filter`} value={filter} onChange={e=>setFilter(e.target.value)} /></Field>
    <Field label="Sources this meaning applies to" htmlFor={`${id}-sources`} hint="Leave empty to infer source applicability from the selected columns."><select id={`${id}-sources`} multiple value={value.source_ids} onChange={e=>set({source_ids:Array.from(e.target.selectedOptions,o=>o.value)})}>{[...new Map(data.catalog.filter(c=>c.source_id).map(c=>[c.source_id!,c.source_name||c.source_id!])).entries()].map(([key,name])=><option key={key} value={key}>{name}</option>)}</select></Field>
    <fieldset className="grounding-column-picker"><legend>Mapped catalog columns</legend>{data.catalog.filter(c=>c.ref.toLowerCase().includes(filter.toLowerCase())).map(c=><label key={c.ref} className="toggle small"><input type="checkbox" checked={value.columns.includes(c.ref)} onChange={e=>set({columns:e.target.checked?[...value.columns,c.ref]:value.columns.filter(x=>x!==c.ref)})}/>{c.ref} · {c.type}</label>)}</fieldset>
    <ul className="list small">{value.columns.filter(c=>!data.catalog.some(x=>x.ref===c)).map(c=><li key={c}>Outdated mapping: {c} <button type="button" className="btn btn-xs" onClick={()=>set({columns:value.columns.filter(x=>x!==c)})}>Remove unavailable column</button></li>)}</ul>
    {value.kind==="metric" && <fieldset><legend>Metric meaning required before hypothesis tests</legend>
      <Field label="Supported calculation" htmlFor={`${id}-calculation`}><select id={`${id}-calculation`} value={metric.calculation??"auto"} onChange={e=>set({metric:{...metric,calculation:e.target.value as "auto"}})}><option value="auto">Use the profiled numeric or boolean outcome</option><option value="numeric_mean">Mean of a numeric outcome</option><option value="flag_rate">Share of records meeting the flag</option><option value="duration_hours">Elapsed hours between two timestamps</option></select></Field>
      <Field label="Outcome column" htmlFor={`${id}-outcome`}><select id={`${id}-outcome`} required value={metric.outcome_column} onChange={e=>set({metric:{...metric,outcome_column:e.target.value}})}>{choices(metric.outcome_column)}</select></Field>
      {metric.calculation==="duration_hours"&&<Field label="Duration end column" htmlFor={`${id}-end`}><select id={`${id}-end`} required value={metric.end_column??""} onChange={e=>set({metric:{...metric,end_column:e.target.value||null}})}>{choices(metric.end_column??"")}</select></Field>}
      {([['numerator','Numerator'],['denominator','Denominator'],['grain','Record grain'],['unit','Unit'],['exclusions','Exclusions']] as const).map(([key,label])=><Field key={key} label={label} htmlFor={`${id}-${key}`}><input id={`${id}-${key}`} required={key!=="exclusions"} value={metric[key]} onChange={e=>set({metric:{...metric,[key]:e.target.value}})}/></Field>)}
      <Field label="Reporting time column" htmlFor={`${id}-time`}><select id={`${id}-time`} value={metric.time_column??""} onChange={e=>set({metric:{...metric,time_column:e.target.value||null}})}>{choices(metric.time_column??"")}</select></Field>
      <Field label="Flag value counted in the numerator" htmlFor={`${id}-true`}><select id={`${id}-true`} value={metric.true_value===true?"true":metric.true_value===false?"false":metric.true_value==null?"":"custom"} onChange={e=>set({metric:{...metric,true_value:e.target.value===""?null:e.target.value==="custom"?"":e.target.value==="true"}})}><option value="">Numeric outcome or native boolean true</option><option value="true">True</option><option value="false">False</option><option value="custom">A reviewed code value</option></select></Field>
      {(typeof metric.true_value==="string"||typeof metric.true_value==="number")&&<Field label="Reviewed flag code" htmlFor={`${id}-code`}><input id={`${id}-code`} required value={metric.true_value} onChange={e=>set({metric:{...metric,true_value:e.target.value}})}/></Field>}
      <p className="small muted">This contract binds an existing outcome column to supported statistical methods. Calculations needing unavailable fields or methods remain proposals.</p>
    </fieldset>}
    {value.predicate && <fieldset><legend>Typed rule</legend><Field label="Rule column" htmlFor={`${id}-rule-column`}><select id={`${id}-rule-column`} required value={value.predicate.column} onChange={e=>set({predicate:{...value.predicate!,column:e.target.value}})}>{choices(value.predicate.column)}</select></Field>
      <Field label="Rule comparison" htmlFor={`${id}-op`}><select id={`${id}-op`} value={value.predicate.op} onChange={e=>set({predicate:{...value.predicate!,op:e.target.value as "="}})}>{["=","!=",">",">=","<","<="].map(op=><option key={op}>{op}</option>)}</select></Field>
      <Field label="Rule value" htmlFor={`${id}-rule-value`}><input id={`${id}-rule-value`} required value={String(value.predicate.value)} onChange={e=>set({predicate:{...value.predicate!,value:e.target.value}})}/></Field>
      <p className="small muted">Population filters constrain hypothesis tests on the mapped table. Quality assertions can be checked on a bounded sample; policy guidance never executes an operational action.</p>
    </fieldset>}
    <div className="grid-2"><Field label="Domain applicability" htmlFor={`${id}-domain`}><select id={`${id}-domain`} value={value.domain} onChange={e=>set({domain:e.target.value})}><option value="">Scoped workspace knowledge</option>{data.domains.map(d=><option key={d}>{d}</option>)}</select></Field>
      <Field label="Review owner" htmlFor={`${id}-owner`}><select id={`${id}-owner`} required={value.state==="approved"} value={value.owner_id??""} onChange={e=>set({owner_id:e.target.value||null})}><option value="">Choose owner</option>{members.map(m=><option key={m.user_id} value={m.user_id}>{m.name||m.user_id}</option>)}</select></Field></div>
    <div className="grid-2"><Field label="Effective from" htmlFor={`${id}-from`}><input type="date" id={`${id}-from`} value={value.effective_from??""} onChange={e=>set({effective_from:e.target.value||null})}/></Field><Field label="Expires on" htmlFor={`${id}-expiry`}><input type="date" id={`${id}-expiry`} value={value.expires_on??""} onChange={e=>set({expires_on:e.target.value||null})}/></Field></div>
    <Field label="Supporting evidence or policy reference" htmlFor={`${id}-evidence`}><textarea id={`${id}-evidence`} required={value.state==="approved"} maxLength={2000} value={value.evidence} onChange={e=>set({evidence:e.target.value})}/></Field>
    <Field label="Validation examples and expected interpretation" htmlFor={`${id}-examples`}><textarea id={`${id}-examples`} required={value.state==="approved"} maxLength={2000} value={value.examples} onChange={e=>set({examples:e.target.value})}/></Field>
    <Field label="Review state" htmlFor={`${id}-state`}><select id={`${id}-state`} value={value.state} onChange={e=>set({state:e.target.value as BindingInput["state"]})}><option value="draft">Draft</option><option value="approved">Reviewed and approved</option><option value="retired">Retired</option></select></Field>
    <div className="btn-row"><button className="btn btn-primary" disabled={busy}>Save mapping</button><button type="button" className="btn" onClick={cancel}>Cancel</button></div>
  </form></Card>;
}

function GoldenQuestions({wsId,canEdit,catalog}: {wsId:string;canEdit:boolean;catalog:Studio["catalog"]}) {
  const data=useAsync(()=>groundingApi.golden(wsId),[wsId]);const act=useAction();const id=useId();
  const [question,setQuestion]=useState("");const [expected,setExpected]=useState<string[]>([]);const [clarify,setClarify]=useState(false);const [model,setModel]=useState(false);
  return <Card title="Golden questions and pilot preparation" label="Golden question evaluation"><p>Save owner-reviewed questions and expected mappings. Each evaluation checks up to 30 questions for grounding and clarification, separately from numerical-answer accuracy and real analyst results.</p>
    <ErrorBox error={data.error} onRetry={data.reload}/><ErrorBox error={act.error}/>
    <ul className="list small">{data.data?.questions.map(q=><li key={q.id}>{q.definition.question} · {q.definition.expect_clarification?"Clarification expected":q.definition.expected_columns.join(", ")}</li>)}</ul>
    {canEdit&&<details><summary>Add a golden question</summary><form className="form" onSubmit={async e=>{e.preventDefault();const r=await act.run(()=>groundingApi.addGolden(wsId,{question,expected_columns:expected,expect_clarification:clarify,notes:"Owner-authored grounding expectation"}));if(r){setQuestion("");setExpected([]);await data.reload();}}}>
      <Field label="Golden question" htmlFor={`${id}-question`}><textarea id={`${id}-question`} minLength={5} required value={question} onChange={e=>setQuestion(e.target.value)}/></Field>
      <Field label="Expected columns" htmlFor={`${id}-expected`}><select id={`${id}-expected`} multiple value={expected} onChange={e=>setExpected(Array.from(e.target.selectedOptions,o=>o.value))}>{catalog.map(c=><option key={c.ref}>{c.ref}</option>)}</select></Field>
      <label className="toggle"><input type="checkbox" checked={clarify} onChange={e=>setClarify(e.target.checked)}/><span>Expect a clarification instead of a guessed definition</span></label>
      <button className="btn" disabled={act.busy}>Save golden question</button></form></details>}
    <label className="toggle"><input type="checkbox" checked={model} onChange={e=>setModel(e.target.checked)}/><span>Include model assistance in this evaluation (uses model budget)</span></label>
    <button className="btn btn-primary" disabled={act.busy||!data.data?.questions.length} onClick={async()=>{await act.run(()=>groundingApi.evaluate(wsId,model));await data.reload();}}>Evaluate grounding</button>
    {data.data?.evaluations.map(e=><details key={e.id}><summary>{e.result.passed} / {e.result.total} grounding checks passed · {e.result.use_model?"Model assisted":"Reviewed knowledge only"}</summary><ul className="list small">{e.result.cases.map(c=><li key={c.id}>{c.passed?"Passed":"Needs review"}: {c.question}</li>)}</ul></details>)}
    <p className="small"><Link to={to.workspace(wsId)}>Record real analyst timings and reviewed outcomes in the workspace pilot scorecard</Link></p>
  </Card>;
}

export function RunGrounding({wsId,runId,onRecheck,refreshKey}: {wsId:string;runId:string;onRecheck:()=>void;refreshKey?:string}) {
  const data=useAsync(()=>groundingApi.run(wsId,runId),[wsId,runId,refreshKey]);const act=useAction();const report=data.data?.grounding;
  return <Card title="Why this context and analysis?" label="Run context explanation"><ErrorBox error={data.error} onRetry={data.reload}/><ErrorBox error={act.error}/>
    {!report?.question&&<p className="small muted">Context resolution is recorded when the context step runs. Older investigations may not have this record.</p>}
    {report?.question&&<><p className="small">{report.used_count} supporting entries resolved from {report.available_count} available entries · model: {report.model_status}</p>
      <ul className="list small">{report.resolutions.map((r,i)=><li key={`${r.id}-${i}`}><strong>{r.term}</strong> · {r.state} · {r.by}<p>{r.columns.join(", ")}</p><p>{r.definition}</p></li>)}</ul>
      {report.needs_clarification&&<Notice tone="warning"><ul>{report.clarification.map(q=><li key={q}>{q}</li>)}</ul><Link to={to.data(wsId,"grounding")}>Review the remaining meaning and mappings</Link><button className="btn btn-sm" disabled={act.busy} onClick={async()=>{const r=await act.run(()=>groundingApi.recheck(wsId,runId));if(r){await data.reload();onRecheck();}}}>Recheck reviewed context</button></Notice>}
      {data.data?.coverage&&<p className="small">Question coverage: {data.data.coverage.covered?"Covered by selected candidates":`Additional exploration requested: ${data.data.coverage.missing.join(", ")}`}</p>}
      {!!report.analysis_filters?.length&&<details><summary>Population and reporting constraints</summary><ul className="list small">{report.analysis_filters.map((f,i)=><li key={i}>{f.column} {f.op} {JSON.stringify(f.value)}</li>)}</ul></details>}
    </>}
    {!!data.data?.brief_receipts.length&&<details><summary>Pinned brief receipts ({data.data.brief_receipts.length})</summary><ul className="list small">{data.data.brief_receipts.map((r,i)=><li key={i}>{r.purpose} · {r.receipt.id} · v{r.receipt.version}</li>)}</ul></details>}
  </Card>;
}
