import type { Artifact } from "../api";

export function outputDisplayName(artifact: Pick<Artifact, "type" | "name" | "content">): string {
  const title = artifact.content?.title;
  return ["dashboard", "report", "report_document"].includes(artifact.type) && typeof title === "string" && title.trim()
    ? title.trim() : artifact.name;
}
