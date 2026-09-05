"use client";

import { useRef, useState } from "react";
import { DailyProvider } from "@daily-co/daily-react";
import SessionView from "@/components/ResumeRoom/SessionView";
import { startResumeRoomSession } from "@/lib/resume-room-api";
import type { SessionInfo } from "@/lib/resumeroom/types";

const SHELL = "min-h-screen bg-[#0a0a0b] text-neutral-100";

export default function Home() {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef(false);

  const enterRoom = async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setStarting(true);
    setError(null);
    try {
      setSession(await startResumeRoomSession());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to start the session");
    } finally {
      setStarting(false);
      inFlight.current = false;
    }
  };

  if (session) {
    return (
      <div className={SHELL}>
        <DailyProvider url={session.roomUrl} token={session.token} subscribeToTracksAutomatically>
          <SessionView roomName={session.roomName} onEnded={() => setSession(null)} />
        </DailyProvider>
      </div>
    );
  }

  return (
    <div className={`${SHELL} flex flex-col items-center justify-center gap-6 px-4`}>
      {error && <p className="max-w-md text-center text-sm text-red-400">{error}</p>}
      <button
        onClick={enterRoom}
        disabled={starting}
        className="rounded-full bg-blue-600 px-8 py-3 text-lg font-semibold text-white hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-60"
      >
        {starting ? "Starting…" : "Enter Room"}
      </button>
    </div>
  );
}
