"use client";

import { useAudioLevel } from "@/lib/resumeroom/useAudioLevel";
import AgentOrbVisual from "@/components/ResumeRoom/AgentOrbVisual";

interface AgentOrbProps {
  /** Daily session id of the bot — lets the orb read its real audio level. */
  sessionId: string;
  speaking: boolean;
}

export default function AgentOrb({ sessionId, speaking }: AgentOrbProps) {
  const levelRef = useAudioLevel(sessionId, { decay: 0.85, interval: 100 });
  return <AgentOrbVisual getLevel={() => levelRef.current} speaking={speaking} />;
}
