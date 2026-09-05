"use client";

import { useLingeringCaption } from "@/lib/resumeroom/useLingeringCaption";
import AgentOrb from "@/components/ResumeRoom/AgentOrb";
import LiveCaption from "@/components/ResumeRoom/LiveCaption";
import type { SpeakerId, TranscriptMessage } from "@/lib/resumeroom/types";

/** Identifies a caption *and* its current text so the linger timer restarts
 *  on every fragment update. */
const captionKey = (m?: TranscriptMessage | null) =>
  m ? `${m.id}:${m.text}` : null;

interface AgentTileProps {
  /** Daily session id of the bot — lets the orb read its real audio level. */
  sessionId: SpeakerId;
  speaking: boolean;
  /** The agent's current TranscriptMessage (or null). */
  caption: TranscriptMessage | null;
  name: string;
}

export default function AgentTile({
  sessionId,
  speaking,
  caption,
  name,
}: AgentTileProps) {
  // Owned here, not in the parent — one legal hook call per component instance.
  const captionVisible = useLingeringCaption(captionKey(caption), speaking);

  return (
    <div className="relative flex min-h-0 min-w-0 flex-1 flex-col items-center justify-center overflow-hidden rounded-2xl bg-[#141416] p-6">
      {/* Blue = agent, mirroring HumanTile's emerald = human. Only mounted while
          the bot actually holds the turn, so the tile is clean when it is idle. */}
      {speaking && (
        <div className="absolute top-4 right-4 flex items-center gap-1.5 rounded-full border border-blue-500/40 bg-blue-500/10 px-3 py-1 text-[11px] font-mono uppercase tracking-widest text-blue-400">
          <span className="h-1.5 w-1.5 rounded-full bg-blue-400" />
          Speaking
        </div>
      )}

      <AgentOrb sessionId={sessionId} speaking={speaking} />

      <LiveCaption text={caption?.text ?? null} visible={captionVisible} />

      <div className="absolute bottom-4 left-4 rounded-lg bg-black/30 px-3 py-2">
        <div className="text-sm font-semibold text-neutral-100">{name}</div>
      </div>
    </div>
  );
}
