"use client";

import { useEffect, useState } from "react";

/** How long a caption stays up after its last update. */
const CAPTION_LINGER_MS = 4000;

/**
 * Whether a caption should still be shown, fading it out once the speaker has
 * been quiet for a while.
 *
 * `key` must change on every caption update — pass something like
 * `${lineId}:${text.length}`. A changing key restarts the timer and brings the
 * caption back, which is why visibility is *derived* from which key was faded
 * rather than stored: setting it eagerly inside the effect is what
 * `react-hooks/set-state-in-effect` rejects.
 *
 * `hold` suppresses the timer entirely — pass the agent's `botSpeaking` so its
 * caption never fades mid-sentence.
 */
export function useLingeringCaption(key: string | null, hold = false): boolean {
  const [fadedKey, setFadedKey] = useState<string | null>(null);

  useEffect(() => {
    if (hold || !key) return;
    const id = setTimeout(() => setFadedKey(key), CAPTION_LINGER_MS);
    return () => clearTimeout(id);
  }, [key, hold]);

  return key !== null && key !== fadedKey;
}
