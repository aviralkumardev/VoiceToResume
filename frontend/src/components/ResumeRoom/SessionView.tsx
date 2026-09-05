"use client";

import { useEffect, useRef, useState } from "react";
import {
  DailyAudio,
  useAppMessage,
  useDaily,
  useDailyError,
  useMeetingState,
  useParticipantIds,
} from "@daily-co/daily-react";
import AgentTile from "@/components/ResumeRoom/AgentTile";
import HumanTile from "@/components/ResumeRoom/HumanTile";
import SessionControls from "@/components/ResumeRoom/SessionControls";
import type { AppMessage, SpeakerId, TranscriptMessage } from "@/lib/resumeroom/types";

const AGENT_NAME = "AI Resume Expert";

/** How long to wait for `agent-ready` before calling the bot dead. Well clear of
 *  the ~6s a healthy start takes, so it only fires on a real failure. */
const AGENT_START_TIMEOUT_MS = 30_000;

interface SessionViewProps {
  roomName: string;
  onEnded: () => void;
}

/** Sentence fragments arrive without reliable surrounding whitespace. */
function joinFragments(existing: string, next: string) {
  if (!existing) return next;
  const needsSpace = !/\s$/.test(existing) && !/^\s/.test(next);
  return existing + (needsSpace ? " " : "") + next;
}

function formatElapsed(totalSeconds: number) {
  const m = Math.floor(totalSeconds / 60)
    .toString()
    .padStart(2, "0");
  const s = Math.floor(totalSeconds % 60)
    .toString()
    .padStart(2, "0");
  return `${m}:${s}`;
}

export default function SessionView({ roomName, onEnded }: SessionViewProps) {
  const daily = useDaily();
  const meetingState = useMeetingState();
  const { meetingError } = useDailyError();
  const [joinError, setJoinError] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<TranscriptMessage[]>([]);
  const [botSpeaking, setBotSpeaking] = useState(false);
  const [agentId, setAgentId] = useState<SpeakerId | null>(null);
  const [agentReady, setAgentReady] = useState(false);
  const [agentTimedOut, setAgentTimedOut] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  // Guards endSession below against running twice: once from the explicit
  // call in SessionControls' End Session handler, and once from the
  // meetingState watcher a moment later when daily.leave() actually resolves.
  const hasEndedRef = useRef(false);

  const endSession = () => {
    if (hasEndedRef.current) return;
    hasEndedRef.current = true;
    onEnded();
  };

  // All participant ids in join order from Daily's own roster — the single
  // source of truth for who is in the room.
  const ids = useParticipantIds({ sort: "joined_at" });

  const joined = meetingState === "joined-meeting";

  // DailyProvider only *creates* the call object from url/token — actually
  // joining (and with it, capturing the mic) is ours to do. Guarding on
  // meetingState keeps React's dev double-mount from firing a second join()
  // on the same call object.
  useEffect(() => {
    if (!daily || daily.isDestroyed()) return;
    if (daily.meetingState() !== "new") return;

    daily.join({ startVideoOff: true, startAudioOff: false }).catch((e) => {
      setJoinError(e instanceof Error ? e.message : "Could not join the room");
    });
  }, [daily]);

  // Clock counts the conversation, not the page — and joining the room isn't the
  // conversation starting: the bot process needs a few seconds more before it
  // can speak. Start on `agent-ready` instead, roughly a second ahead of the
  // greeting.
  useEffect(() => {
    if (!agentReady) return;
    const id = setInterval(() => setElapsed((e) => e + 1), 1000);
    return () => clearInterval(id);
  }, [agentReady]);

  // Without this a bot that died on startup looks exactly like a slow one — the
  // wait indicator would just spin for the rest of the session.
  useEffect(() => {
    if (!joined || agentReady) return;
    const id = setTimeout(() => setAgentTimedOut(true), AGENT_START_TIMEOUT_MS);
    return () => clearTimeout(id);
  }, [joined, agentReady]);

  // Catches every way the call can end that the candidate didn't click through
  // here themselves: the bot's own graceful sign-off, the session's hard
  // timeout ceiling, the empty-room teardown, or an admin /stop. In all of
  // those the backend ends the pipeline and deletes the Daily room out from
  // under us. If we'd already left (or never joined), that's the same
  // terminal "left-meeting" state daily.leave() produces for a
  // candidate-initiated End Session — but deleting a room a client is still
  // joined to doesn't produce "left-meeting" for that client; Daily surfaces
  // it as the fatal "error" state instead (see useDailyError's errorMsg,
  // which is what the red banner below renders). Both are handled here so
  // one watcher covers every path.
  useEffect(() => {
    if (meetingState === "left-meeting" || meetingState === "error") {
      endSession();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meetingState]);

  useAppMessage({
    onAppMessage: (ev) => {
      const d = ev.data as AppMessage;
      if (d?.type === "transcript") {
        // A turn arrives as many messages: the agent word by word as it speaks,
        // the candidate as a transcript that grows while they talk. Both fold
        // into the open line for that turn so there's one line per turn —
        // appended for the agent's fragments, swapped in for the candidate's
        // cumulative text.
        setTranscript((t) => {
          const last = t[t.length - 1];
          const merged = !!(last && last.speaker === d.speaker && last.turn === d.turn);
          // eslint-disable-next-line no-console
          console.log("[ResumeRoom] transcript message", {
            incoming: { speaker: d.speaker, turn: d.turn, text: d.text, replace: d.replace },
            last: last ? { speaker: last.speaker, turn: last.turn, text: last.text } : null,
            merged,
          });
          if (merged) {
            const text = d.replace ? d.text : joinFragments(last.text, d.text);
            return [...t.slice(0, -1), { ...last, text }];
          }
          return [...t, { id: t.length, speaker: d.speaker, text: d.text, turn: d.turn }];
        });
      } else if (d?.type === "speaking") {
        // The bot's speaking state, keyed by its Daily session id.
        // eslint-disable-next-line no-console
        console.log("[ResumeRoom] speaking message", { agentId, speaker: d.speaker, value: d.value });
        if (agentId && d.speaker === agentId) {
          setBotSpeaking(d.value);
        }
      } else if (d?.type === "agent-ready") {
        setAgentId(d.participantId);
        setAgentReady(true);
      }
    },
  });

  const error =
    joinError ??
    meetingError?.errorMsg ??
    (agentTimedOut ? "The agent didn't start — check the bot log." : null) ??
    null;

  // Build a map of speakerId → last TranscriptMessage for that speaker.
  const lastLineFor = (id: SpeakerId): TranscriptMessage | null => {
    let result: TranscriptMessage | null = null;
    for (const m of transcript) {
      if (m.speaker === id) result = m;
    }
    return result;
  };

  return (
    // h-screen, not min-h-screen: the call is a fixed shell, so the controls
    // are always on screen and the tiles take whatever height is left over.
    <div className="mx-auto flex h-screen max-w-5xl flex-col px-4 py-6">
      <header className="flex items-center justify-between border-b border-neutral-800 pb-4">
        <div className="flex items-center gap-3 font-mono text-sm uppercase tracking-widest text-neutral-300">
          <span>Meeting Room</span>
        </div>
        {/* Two-stage wait, so it's obvious which half is slow: joining the room
            takes ~1s, then the bot process spends a few more seconds starting. */}
        <div className="flex items-center gap-2 font-mono text-sm text-neutral-300">
          {agentReady ? (
            <>
              <span className="h-2 w-2 rounded-full bg-red-500" />
              {formatElapsed(elapsed)}
            </>
          ) : (
            <>
              <span className="h-2 w-2 animate-pulse rounded-full bg-amber-400" />
              <span className="text-neutral-400">
                {joined ? `${AGENT_NAME} Joining…` : "Connecting…"}
              </span>
            </>
          )}
        </div>
      </header>

      {error && (
        <p className="mt-4 rounded-lg border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-300">
          {error}
        </p>
      )}

      {/* The stage. flex-1 absorbs all the height the header and controls don't
          use; min-h-0 lets it give height back on a short window instead of
          pushing the controls off screen. Always exactly two participants
          (agent + candidate), so a plain two-item row replaces pitch_room's
          best-fit video grid. */}
      <div className="mt-6 flex min-h-0 flex-1 gap-4">
        {ids.map((id) =>
          id === agentId ? (
            <AgentTile
              key={id}
              sessionId={id}
              speaking={botSpeaking}
              caption={lastLineFor(id)}
              name={AGENT_NAME}
            />
          ) : (
            <HumanTile key={id} sessionId={id} caption={lastLineFor(id)} />
          ),
        )}
      </div>

      {/* Same rule as under the header, closing the participants off from the
          controls. */}
      <div className="mt-6 border-t border-neutral-800 pt-6">
        <SessionControls roomName={roomName} onEnded={endSession} />
      </div>

      <DailyAudio />
    </div>
  );
}
