/**
 * Control-bar icons. Inline SVG rather than an icon dependency — there are only
 * a handful, and they all share one geometry: a 24-unit box, 1.5 stroke, round
 * caps, `currentColor`, so the button's text colour drives them.
 *
 * "Off" states are the same glyph with a diagonal slash, which reads faster at
 * 20px than a separate crossed-out drawing.
 */

type IconProps = { className?: string };

function Svg({ className, children }: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      className={className ?? "h-5 w-5"}
    >
      {children}
    </svg>
  );
}

const Slash = () => <path d="M4 4 20 20" />;

const micBody = (
  <>
    <path d="M12 2.75a2.75 2.75 0 0 0-2.75 2.75v6a2.75 2.75 0 0 0 5.5 0v-6A2.75 2.75 0 0 0 12 2.75Z" />
    <path d="M18.5 11v.5a6.5 6.5 0 0 1-13 0V11" />
    <path d="M12 18v3.25" />
    <path d="M8.75 21.25h6.5" />
  </>
);

export const MicIcon = (p: IconProps) => <Svg {...p}>{micBody}</Svg>;

export const MicOffIcon = (p: IconProps) => (
  <Svg {...p}>
    {micBody}
    <Slash />
  </Svg>
);

/** Leave-the-room arrow, for ending the session. */
export const LeaveIcon = (p: IconProps) => (
  <Svg {...p}>
    <path d="M9.5 4.25H6.75A2.25 2.25 0 0 0 4.5 6.5v11a2.25 2.25 0 0 0 2.25 2.25H9.5" />
    <path d="M15.75 8.25 19.5 12l-3.75 3.75" />
    <path d="M19.5 12h-9" />
  </Svg>
);
