/** Daily session id — present on every frame, stable for the participant's
 *  lifetime. Used as the speaker discriminator on the wire and as tile keys. */
export type SpeakerId = string;

export interface TranscriptMessage {
  id: number;
  /** Daily session id of the speaking participant. */
  speaker: SpeakerId;
  text: string;
  /**
   * Opaque id, stable for one speaking turn; fragments of one turn share it.
   * Agent turns use the TTS context id, user turns a counter — compare only for
   * equality.
   */
  turn: string;
}

export type AppMessage =
  | {
      type: "transcript";
      /** Daily session id. */
      speaker: SpeakerId;
      text: string;
      turn: string;
      /**
       * True when `text` is the whole turn so far and supersedes what is already
       * on the line — the candidate's cumulative STT transcript. False when it is
       * a fragment to append — the agent's per-word TTS stream.
       */
      replace: boolean;
    }
  | {
      /** The agent started or stopped producing audio. */
      type: "speaking";
      /** Daily session id of the bot. */
      speaker: SpeakerId;
      value: boolean;
    }
  | {
      /** The bot has joined and queued its greeting. */
      type: "agent-ready";
      /** Daily session id of the bot participant — lets the frontend key the agent tile. */
      participantId: SpeakerId;
    };

export interface SessionInfo {
  roomUrl: string;
  token: string;
  roomName: string;
}
