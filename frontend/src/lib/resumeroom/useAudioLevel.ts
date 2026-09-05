"use client";

import { useCallback, useRef } from "react";
import { useAudioLevelObserver } from "@daily-co/daily-react";

interface UseAudioLevelOptions {
  /** Exponential decay factor applied when volume drops — same formula as
   *  HumanTile's MicLevelRing: instant rise, slow decay. */
  decay?: number;
  /** Passed straight through to Daily's observer callback frequency. */
  interval?: number;
}

/**
 * Live audio level for `sessionId`, attack-instant / decay-exponential
 * smoothed, exposed as a ref rather than state so 60fps consumers (rAF
 * loops) can sample it without forcing a React re-render every tick.
 */
export function useAudioLevel(
  sessionId: string,
  { decay = 0.85, interval }: UseAudioLevelOptions = {},
) {
  const levelRef = useRef(0);

  useAudioLevelObserver(
    sessionId,
    useCallback(
      (volume: number) => {
        // eslint-disable-next-line no-console
        console.log("[ResumeRoom] audio level", { sessionId, volume });
        if (!Number.isFinite(volume)) return;
        levelRef.current =
          volume > levelRef.current ? volume : levelRef.current * decay;
      },
      [decay, sessionId],
    ),
    undefined,
    interval,
  );

  return levelRef;
}
