"use client";

import { useCallback, useState } from "react";
import { useAudioLevelObserver, useLocalSessionId, useParticipantProperty } from "@daily-co/daily-react";
import { useLingeringCaption } from "@/lib/resumeroom/useLingeringCaption";
import LiveCaption from "@/components/ResumeRoom/LiveCaption";
import type { TranscriptMessage } from "@/lib/resumeroom/types";

/** Identifies a caption and its current text so the linger timer restarts on
 *  every fragment update — same logic as AgentTile. */
const captionKey = (m?: TranscriptMessage | null) =>
  m ? `${m.id}:${m.text}` : null;

/** Derive up-to-two initials from a display name, falling back to "?". */
function initials(name: string | undefined): string {
  if (!name) return "?";
  const parts = name.trim().split(/\s+/);
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

/** Live mic level ring, sized relative to the --orb CSS variable. */
function MicLevelRing({ sessionId }: { sessionId: string }) {
  const [level, setLevel] = useState(0);

  useAudioLevelObserver(
    sessionId,
    useCallback((volume: number) => {
      setLevel((prev) => {
        if (!Number.isFinite(volume)) return prev;
        // Rise instantly, decay slowly to smooth out the instantaneous 0s
        return volume > prev ? volume : prev * 0.85;
      });
    }, []),
  );

  const speaking = level > 0.005;
  const grow = Math.min(level * 5, 1);

  return (
    <>
      <span
        className="absolute h-[var(--orb)] w-[var(--orb)] rounded-full border border-emerald-400/60 transition-none"
        style={{
          transform: `scale(${1 + grow * 0.41})`,
          opacity: speaking ? 0.25 + grow * 0.75 : 0,
        }}
      />
      {speaking && (
        <span className="absolute -top-4 right-0 flex items-center gap-1.5 rounded-full border border-emerald-500/40 bg-emerald-500/10 px-3 py-1 font-mono text-[11px] uppercase tracking-widest text-emerald-400">
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
          Speaking
        </span>
      )}
    </>
  );
}

interface HumanTileProps {
  /** Daily session id for this participant (local or remote). */
  sessionId: string;
  /** This participant's current TranscriptMessage (or null). */
  caption: TranscriptMessage | null;
}

/**
 * Tile for the candidate — the only human in the room. No video support, so
 * this is always the initials-avatar + mic-level-ring presentation.
 */
export default function HumanTile({ sessionId, caption }: HumanTileProps) {
  const localId = useLocalSessionId();
  const isLocal = sessionId === localId;

  const micOn = useParticipantProperty(sessionId, "audio");
  const userName = useParticipantProperty(sessionId, "user_name") as
    | string
    | undefined;

  const displayName = isLocal
    ? `${userName ?? "You"} · You`
    : (userName ?? "Participant");

  const captionVisible = useLingeringCaption(captionKey(caption));

  return (
    <div className="relative flex min-h-0 min-w-0 flex-1 flex-col items-center justify-center overflow-hidden rounded-2xl bg-[#141416] p-6">
      {!micOn && (
        <div className="absolute top-4 right-4 flex items-center gap-1.5 rounded-full border border-neutral-500/40 bg-black/40 px-3 py-1 font-mono text-[11px] uppercase tracking-widest text-neutral-400">
          <span className="h-1.5 w-1.5 rounded-full bg-neutral-400" />
          Muted
        </div>
      )}

      <div className="relative flex items-center justify-center">
        {micOn && <MicLevelRing sessionId={sessionId} />}
        <div className="flex h-[var(--orb)] w-[var(--orb)] items-center justify-center rounded-full bg-neutral-700">
          <span className="text-2xl font-semibold tracking-wide text-neutral-300">
            {initials(userName)}
          </span>
        </div>
      </div>

      <LiveCaption text={caption?.text ?? null} visible={captionVisible} />

      <div className="absolute bottom-4 left-4 rounded-lg bg-black/30 px-3 py-2">
        <div className="text-sm font-semibold text-neutral-100">{displayName}</div>
      </div>
    </div>
  );
}
