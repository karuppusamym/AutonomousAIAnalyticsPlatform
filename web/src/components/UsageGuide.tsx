import { Link } from "react-router-dom";
import { to } from "../routes";

/** Keep the everyday journey beside the work, without adding another destination. */
export function UsageGuide({wsId}: {wsId: string}) {
  return <details className="usage-guide">
    <summary>New here? How to get an answer you can trust</summary>
    <p>Ask checks a focused question. An investigation explores possible explanations, tests them against your data and records the evidence.</p>
    <ol className="usage-steps">
      <li><strong>Connect your data.</strong> <Link to={to.sources(wsId)}>Add a source or upload a file</Link>, select the tables and check that the source is ready.</li>
      <li><strong>Explain the business meaning.</strong> <Link to={to.data(wsId,"grounding")}>Review definitions and column mappings</Link>. The model reads applicable knowledge first; an owner reviews new meanings and calculations.</li>
      <li><strong>Ask a clear question.</strong> Include the outcome, people or records to include, and reporting period. Use <Link to={to.ask(wsId)}>Ask</Link> for a focused answer, or Start work for an investigation.</li>
      <li><strong>Review what needs you.</strong> Check the proposed data and plan. If a meaning is unresolved, update its definition, then recheck context in the investigation. Review pending decisions in the <Link to={to.approvals(wsId)}>approval inbox</Link>.</li>
      <li><strong>Read the evidence and decide.</strong> Review findings, limitations and follow-up actions. Publishing follows the workspace approval policy.</li>
    </ol>
    <details><summary>What do the results mean?</summary>
      <dl className="usage-terms">
        <dt>Hypothesis</dt><dd>A possible explanation that the app can test. It is a question to investigate, not a fact.</dd>
        <dt>Supported</dt><dd>The test found evidence for this explanation under its assumptions. Check verification before acting on it.</dd>
        <dt>Rejected</dt><dd>This test did not support the proposed explanation. It does not prove the opposite is always true.</dd>
        <dt>Inconclusive</dt><dd>The available data or test could not decide.</dd>
        <dt>Verified finding</dt><dd>A reported result passed the app’s required checks. Its data, assumptions and limitations still matter.</dd>
        <dt>Mapping</dt><dd>A link between a business meaning, such as “SLA breach”, and the data fields that represent it.</dd>
      </dl>
    </details>
  </details>;
}
