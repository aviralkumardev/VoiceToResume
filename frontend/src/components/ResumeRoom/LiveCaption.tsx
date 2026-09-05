"use client";

import { useEffect, useRef } from "react";

interface LiveCaptionProps {
  text: string | null;
  /** False once the speaker has been quiet a while — fades the bubble out. */
  visible: boolean;
}

/**
 * A speech caption: a fixed two-line window onto the current turn, showing the
 * newest words and clipping older ones off the top. This is the only transcript
 * on screen during the call.
 *
 * Bottom-anchoring is done by setting `scrollTop` explicitly rather than by
 * leaning on how browsers pick the initial scroll position of a
 * `flex-col-reverse` container.
 */
export default function LiveCaption({ text, visible }: LiveCaptionProps) {
  const windowRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = windowRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [text]);

  if (!text) return null;

  return (
    <div
      aria-hidden={!visible}
      className={`relative mt-6 max-w-[70%] rounded-xl bg-black/40 px-4 py-3 text-center text-sm leading-6 text-neutral-200 transition-opacity duration-700 ${
        visible ? "opacity-100" : "opacity-0"
      }`}
    >
      {/* max-h is exactly 2 × leading-6, inside the bubble's padding so py-3
          doesn't eat a line. */}
      <div ref={windowRef} className="max-h-12 overflow-hidden">
        {text}
      </div>
    </div>
  );
}
