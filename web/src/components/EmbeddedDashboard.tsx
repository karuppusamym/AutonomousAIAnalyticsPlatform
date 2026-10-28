import { useEffect, useRef, useState } from "react";
import { api, errorMessage, type EmbedToken } from "../api";
import { ErrorBox, Loading } from "./ui";

const MIN_HEIGHT = 480;

/** A published Superset dashboard shown in place (S08-T5). Every token, including each refresh the SDK asks
 * for, comes from `POST /api/v2/dashboards/{id}/embed-token`, which re-checks the viewer's access and puts
 * their own row filters in the token; nothing here decides access. */
export function EmbeddedDashboard({ artifactId, title }: { artifactId: string; title: string }) {
  const mount = useRef<HTMLDivElement>(null);
  const [first, setFirst] = useState<EmbedToken | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let unmount: (() => void) | null = null;
    let sizer: ReturnType<typeof setInterval> | null = null;
    setError(null);
    setFirst(null);
    (async () => {
      let initial: EmbedToken;
      try {
        initial = await api.builderEmbedToken(artifactId);  // refusals (audience, withheld data) surface here
      } catch (e) {
        if (!cancelled) setError(errorMessage(e));
        return;
      }
      if (cancelled || !mount.current) return;
      if (!initial.superset_url) {
        setError("Superset's address is not configured on the platform (ANALYSTOS_SUPERSET_PUBLIC_URL).");
        return;
      }
      setFirst(initial);
      let pending: string | null = initial.token;  // the SDK's first request uses the token already minted
      try {
        const { embedDashboard } = await import("@superset-ui/embedded-sdk");
        if (cancelled || !mount.current) return;
        const dashboard = await embedDashboard({
          id: initial.dashboard_uuid,
          supersetDomain: initial.superset_url.replace(/\/+$/, ""),
          mountPoint: mount.current,
          fetchGuestToken: async () => {
            if (pending) { const t = pending; pending = null; return t; }
            return (await api.builderEmbedToken(artifactId)).token;
          },
          dashboardUiConfig: { hideTitle: true, filters: { expanded: false } },
          iframeTitle: `${title} (Superset)`,
        });
        if (cancelled) { dashboard.unmount(); return; }
        unmount = dashboard.unmount;
        const fit = async () => {
          const frame = mount.current?.querySelector("iframe");
          if (!frame) return;
          try {
            const { height } = await dashboard.getScrollSize();
            frame.style.height = `${Math.max(MIN_HEIGHT, Math.ceil(height))}px`;
          } catch { /* the frame is still loading */ }
        };
        sizer = setInterval(() => void fit(), 2000);
      } catch (e) {
        if (!cancelled) setError(errorMessage(e));
      }
    })();
    return () => {
      cancelled = true;
      if (sizer) clearInterval(sizer);
      unmount?.();
      if (mount.current) mount.current.replaceChildren();
    };
  }, [artifactId, title, attempt]);

  return (
    <div className="embedded-dashboard">
      {!first && !error && <Loading label="Opening the dashboard in Superset…" />}
      <ErrorBox error={error} onRetry={() => setAttempt((n) => n + 1)} />
      {first && (
        <p className="small muted">
          Shown with your access{first.rls_clauses > 0
            ? `: ${first.rls_clauses} row filter${first.rls_clauses === 1 ? "" : "s"} from your workspace policy applied.`
            : "."}
        </p>
      )}
      <div ref={mount} className="embedded-dashboard-frame" hidden={!!error} data-testid="superset-embed" />
    </div>
  );
}
