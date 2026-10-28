import { useRef, useState, type CSSProperties, type ReactNode } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { to } from "../routes";
import "./lite.css";

/** A second presentation of the existing workspace, using the same routes and permissions. */
export function LiteWorkspace({ wsId, name, onExit, children }: {
  wsId: string; name?: string; onExit: () => void; children: ReactNode;
}) {
  const [contextOpen, setContextOpen] = useState(true);
  const [navigationOpen, setNavigationOpen] = useState(true);
  const [leftWidth, setLeftWidth] = useState(220);
  const [rightWidth, setRightWidth] = useState(300);
  const drag = useRef<{ side: "left" | "right"; x: number; width: number } | null>(null);
  const resize = (side: "left" | "right", value: number) => {
    const width = Math.round(Math.max(side === "left" ? 160 : 240, Math.min(side === "left" ? 320 : 480, value)));
    if (side === "left") setLeftWidth(width); else setRightWidth(width);
  };
  const divider = (side: "left" | "right") => <div
    className={`lite-resizer lite-resizer-${side}`} role="separator" tabIndex={0}
    aria-label={`Resize ${side === "left" ? "navigation" : "workflow"} panel`} aria-orientation="vertical"
    aria-valuemin={side === "left" ? 160 : 240} aria-valuemax={side === "left" ? 320 : 480}
    aria-valuenow={side === "left" ? leftWidth : rightWidth}
    onPointerDown={(e) => {
      if (e.button !== 0) return;
      drag.current = { side, x: e.clientX, width: side === "left" ? leftWidth : rightWidth };
      e.currentTarget.setPointerCapture(e.pointerId);
      e.preventDefault();
    }}
    onPointerMove={(e) => {
      const start = drag.current;
      if (start?.side === side) resize(side, start.width + (e.clientX - start.x) * (side === "left" ? 1 : -1));
    }}
    onPointerUp={(e) => { drag.current = null; e.currentTarget.releasePointerCapture(e.pointerId); }}
    onPointerCancel={() => { drag.current = null; }} onLostPointerCapture={() => { drag.current = null; }}
    onDoubleClick={() => resize(side, side === "left" ? 220 : 300)}
    onKeyDown={(e) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
      e.preventDefault();
      const current = side === "left" ? leftWidth : rightWidth;
      resize(side, e.key === "Home" ? 0 : e.key === "End" ? 999 : current + (e.key === "ArrowRight" ? 20 : -20) * (side === "left" ? 1 : -1));
    }} />;
  const [historyOpen, setHistoryOpen] = useState(false);
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const location = useLocation();
  const sections = [
    { label: "Conversation", icon: "◇", href: to.ask(wsId), detail: "Ask, follow up, and turn answers into investigations." },
    { label: "Data", icon: "▤", href: to.sources(wsId), detail: "Connect a database and choose the tables in scope." },
    { label: "Metadata", icon: "⌘", href: to.catalog(wsId), detail: "Review schemas, relationships, and business definitions." },
    { label: "Investigations", icon: "◎", href: to.work(wsId), detail: "Explore drivers and review the evidence behind findings." },
    { label: "Results", icon: "▥", href: to.outputs(wsId), detail: "Build dashboards, reports, and reusable datasets." },
    { label: "Publish", icon: "↗", href: to.approvals(wsId), detail: "Review publication requests. Publish dashboards to Superset through the existing approval flow." },
  ];
  const isAsk = location.pathname === to.ask(wsId) || location.pathname === to.lite(wsId);
  const current = sections.find((s) => location.pathname.startsWith(s.href)) ?? { label: isAsk ? "Conversation" : "Workspace" };
  return (
    <div style={{ "--lite-left": `${leftWidth}px`, "--lite-right": `${rightWidth}px` } as CSSProperties}
      className={`lite-workspace ${navigationOpen ? "lite-with-navigation" : ""} ${contextOpen ? "lite-with-context" : ""} ${historyOpen ? "lite-history-open" : ""} ${evidenceOpen ? "lite-evidence-open" : ""}`}>
      <a href="#main" className="skip-link">Skip to content</a>
      <header className="lite-header">
        <Link to={to.workspaces()} className="lite-brand">a<span>AnalystOS</span></Link>
        <span className="lite-label">LITE</span><span className="lite-divider" />
        <Link to={to.workspace(wsId)} className="lite-workspace-name">{name ?? "Workspace"}</Link>
        <div className="lite-header-actions">
          <button className="btn btn-sm btn-ghost" onClick={() => setNavigationOpen(!navigationOpen)} aria-expanded={navigationOpen} aria-controls="lite-navigation">Navigation</button>
          <button className="btn btn-sm btn-ghost" onClick={() => setContextOpen(!contextOpen)} aria-expanded={contextOpen} aria-controls="lite-context">Workflow</button>
          <button className="btn btn-sm" onClick={() => {
            const focus = navigationOpen || contextOpen;
            setNavigationOpen(!focus); setContextOpen(!focus);
          }}>{navigationOpen || contextOpen ? "Focus mode" : "Three panels"}</button>
          <button className="btn btn-sm" onClick={onExit}>Full interface</button>
        </div>
      </header>
      <nav id="lite-navigation" className="lite-rail" aria-label="Lite workspace" hidden={!navigationOpen}>
        <div className="lite-panel-title">Workspace tools</div>
        {sections.map((s) => <NavLink key={s.label} to={s.href} end={s.label === "Investigations"} className={({ isActive }) => `lite-rail-link ${isActive || (isAsk && s.label === "Conversation") ? "active" : ""}`}>
          <span aria-hidden="true">{s.icon}</span><span>{s.label}</span>
        </NavLink>)}
        <Link className="lite-rail-link lite-rail-bottom" to={to.workspace(wsId)}><span aria-hidden="true">⊞</span><span>Overview</span></Link>
      </nav>
      {navigationOpen && divider("left")}
      <div className="lite-editor">
        <div className="lite-editor-bar"><span>{current.label}</span><span className="lite-editor-hint">Your data. One workspace.</span>
          {isAsk && <div className="lite-tools">
            <button className="btn btn-xs" aria-expanded={historyOpen} onClick={() => setHistoryOpen(!historyOpen)}>History</button>
            <button className="btn btn-xs" aria-expanded={evidenceOpen} onClick={() => setEvidenceOpen(!evidenceOpen)}>Evidence & SQL</button>
          </div>}
        </div>
        <main id="main" tabIndex={-1} className="lite-content">{children}</main>
      </div>
      {contextOpen && divider("right")}
      {contextOpen && <aside className="lite-context" id="lite-context" aria-label="Workflow guide">
        <div className="lite-context-heading"><strong>From data to decision</strong><button className="btn btn-xs" onClick={() => setContextOpen(false)} aria-label="Close workflow guide">×</button></div>
        <p>Keep the question at the center. Open the tools you need along the way.</p>
        <ol>{sections.slice(1).map((s, i) => <li key={s.label}><Link to={s.href}><span>{i + 1}</span>{s.label}</Link><p>{s.detail}</p></li>)}</ol>
        <Link className="btn" to={to.ask(wsId)}>Back to conversation</Link>
      </aside>}
      <footer className="lite-status"><span>Database → Metadata → Analysis → Output → Superset</span><span>Governed workspace</span></footer>
    </div>
  );
}
