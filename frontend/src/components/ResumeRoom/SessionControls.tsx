"use client";

import { useDaily, useLocalSessionId, useParticipantProperty } from "@daily-co/daily-react";
import { LeaveIcon, MicIcon, MicOffIcon } from "@/components/ResumeRoom/icons";
import { stopResumeRoomSession } from "@/lib/resume-room-api";

interface SessionControlsProps {
  roomName: string;
  onEnded: () => void;
}

type ToggleKind = "neutral" | "danger";

const TOGGLE_STYLES: Record<ToggleKind, string> = {
  neutral: "border-white/10 bg-white/5 text-neutral-300 hover:bg-white/10 hover:text-white",
  danger: "border-red-400/30 bg-red-400/15 text-red-300 hover:bg-red-400/25",
};

interface ToggleProps {
  label: string;
  pressed: boolean;
  kind: ToggleKind;
  onClick: () => void;
  children: React.ReactNode;
}

function Toggle({ label, pressed, kind, onClick, children }: ToggleProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      aria-pressed={pressed}
      title={label}
      className={`flex h-11 w-11 items-center justify-center rounded-full border transition-colors focus-visible:ring-2 focus-visible:ring-white/30 focus-visible:outline-none ${TOGGLE_STYLES[kind]}`}
    >
      {children}
    </button>
  );
}

export default function SessionControls({ roomName, onEnded }: SessionControlsProps) {
  const daily = useDaily();
  const localId = useLocalSessionId();

  // Mic state comes from Daily — toggled via setLocalAudio.
  const audioOn = useParticipantProperty(localId, "audio");
  const muted = !audioOn;

  const toggleMic = () => {
    daily?.setLocalAudio(muted);
  };

  const endSession = async () => {
    try {
      await stopResumeRoomSession(roomName);
    } finally {
      await daily?.leave();
      onEnded();
    }
  };

  return (
    <div className="flex items-center justify-center gap-3">
      <div className="flex items-center gap-2 rounded-full border border-white/10 bg-white/[0.03] p-2 backdrop-blur-sm">
        <Toggle
          label={muted ? "Unmute microphone" : "Mute microphone"}
          pressed={muted}
          kind={muted ? "danger" : "neutral"}
          onClick={toggleMic}
        >
          {muted ? <MicOffIcon /> : <MicIcon />}
        </Toggle>
      </div>

      <button
        type="button"
        onClick={endSession}
        className="flex items-center gap-2 rounded-full bg-red-600/90 px-5 py-2.5 text-sm font-medium text-white shadow-lg shadow-red-950/40 transition-colors hover:bg-red-500 focus-visible:ring-2 focus-visible:ring-red-300/50 focus-visible:outline-none"
      >
        <LeaveIcon className="h-4 w-4" />
        End Session
      </button>
    </div>
  );
}
