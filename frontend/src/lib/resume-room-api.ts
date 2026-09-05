import type { SessionInfo } from "@/lib/resumeroom/types";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

// Thrown by startResumeRoomSession so callers can branch on `status` (e.g. 429
// when the concurrency cap is hit) without string-matching `message`.
export class ResumeRoomApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ResumeRoomApiError";
    this.status = status;
  }
}

interface ValidationErrorItem {
  loc?: unknown[];
  msg?: string;
}

function errorMessage(body: unknown, status: number): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    const parts = (detail as ValidationErrorItem[])
      .map(d => {
        const field = Array.isArray(d?.loc) ? d.loc.filter((x: unknown) => x !== "query").join(".") : "";
        const msg = d?.msg ?? "invalid";
        return field ? `${field}: ${msg}` : String(msg);
      })
      .filter(Boolean);
    if (parts.length > 0) return `Request rejected (${status}) — ${parts.join("; ")}`;
  }
  return `Request failed: ${status}`;
}

export async function startResumeRoomSession(): Promise<SessionInfo> {
  const res = await fetch(`${API_BASE_URL}/resume-room/start`, { method: "POST" });

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ResumeRoomApiError(errorMessage(body, res.status), res.status);
  }

  return res.json();
}

export async function stopResumeRoomSession(roomName: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/resume-room/stop/${roomName}`, {
    method: "POST",
  });

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(errorMessage(body, res.status));
  }
}
