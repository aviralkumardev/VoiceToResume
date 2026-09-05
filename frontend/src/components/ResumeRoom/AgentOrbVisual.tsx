"use client";

import { useEffect, useRef, useState } from "react";
import { ORB_FRAG, ORB_VERT } from "@/components/ResumeRoom/orbShader";

/** Level smoothing time constants, in SECONDS — not per-frame lerp factors, so
 *  the motion is identical on a 60Hz and a 144Hz display.
 *
 *  Asymmetric on purpose: a gentle rise still catches the start of a sentence,
 *  while a much slower fall holds the energy through the gaps between syllables
 *  instead of dropping into them. Daily reports a new level every 100ms and
 *  speech is spiky, so tracking it closely (this was a symmetric per-frame
 *  0.18) makes the orb chatter. */
const LEVEL_ATTACK_TAU = 0.21;
const LEVEL_RELEASE_TAU = 0.55;
/** Raw audio level that counts as "full". */
const LEVEL_GAIN = 5;
/** Flow/spin rates in phase-units per second, at silence and at full level.
 *  The BASE values set the idle look and shouldn't change. The GAINs are
 *  deliberately modest — speech should lift the pace, not make it busy. */
const FLOW_BASE = 0.055;
const FLOW_GAIN = 0.075;
const SPIN_BASE = 0.035;
const SPIN_GAIN = 0.045;
/** Outward pulse-wave cadence, in waves per second. The base is small but
 *  non-zero so a wave already in flight keeps travelling as speech stops,
 *  rather than freezing mid-air (its amplitude is scaled by level in the
 *  shader, so it fades out where it is). */
const PULSE_BASE = 0.18;
const PULSE_GAIN = 0.45;
/** Frame-delta ceiling. rAF pauses in a background tab, so the first frame
 *  back can carry a delta of many seconds — without this the phases integrate
 *  that in one step and the orb visibly jumps on tab focus. */
const MAX_DT = 1 / 20;
/** Floor applied while `speaking` so the orb keeps some energy between words
 *  instead of going inert every time the TTS pauses for breath. */
const SPEAKING_FLOOR = 0.18;
/** Backing-store cap. 3x on a ~170px orb is ~1MP of fragment work, which is
 *  nothing for this shader, and the extra resolution is what keeps the disc's
 *  hard edge clean rather than slightly soft/aliased. */
const MAX_DPR = 3;

interface AgentOrbVisualProps {
  /** Sampled once per animation frame; expected range ~0..1 (unclamped ok). */
  getLevel: () => number;
  /** Keeps a baseline energy floor while the agent holds the turn. */
  speaking: boolean;
}

function compile(gl: WebGLRenderingContext, type: number, src: string) {
  const sh = gl.createShader(type);
  if (!sh) return null;
  gl.shaderSource(sh, src);
  gl.compileShader(sh);
  if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
    console.error("[AgentOrb] shader compile failed:", gl.getShaderInfoLog(sh));
    gl.deleteShader(sh);
    return null;
  }
  return sh;
}

/**
 * The AI agent orb — a WebGL-rendered disc of flowing aurora borealis curtains.
 *
 * Pure presentation: no Daily import, so it can be driven by a live call (via
 * AgentOrb) or by a dev harness. The audio level is read through `getLevel`
 * once per frame and pushed straight into a shader uniform, so nothing here
 * re-renders React at frame rate.
 *
 * Falls back to a static CSS gradient disc if WebGL is unavailable.
 */
export default function AgentOrbVisual({ getLevel, speaking }: AgentOrbVisualProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [glFailed, setGlFailed] = useState(false);

  // Mirrored into refs so the render loop can mount once and still see the
  // latest values — AgentTile re-renders on every caption fragment. Synced in
  // an effect (runs after every commit) rather than during render itself.
  const getLevelRef = useRef(getLevel);
  const speakingRef = useRef(speaking);
  useEffect(() => {
    getLevelRef.current = getLevel;
    speakingRef.current = speaking;
  });

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const gl =
      canvas.getContext("webgl", {
        alpha: true,
        premultipliedAlpha: false,
        antialias: false,
        depth: false,
        stencil: false,
      }) ?? null;

    if (!gl) {
      setGlFailed(true);
      return;
    }

    const vs = compile(gl, gl.VERTEX_SHADER, ORB_VERT);
    const fs = compile(gl, gl.FRAGMENT_SHADER, ORB_FRAG);
    const prog = vs && fs ? gl.createProgram() : null;
    if (!vs || !fs || !prog) {
      setGlFailed(true);
      return;
    }

    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      console.error("[AgentOrb] program link failed:", gl.getProgramInfoLog(prog));
      setGlFailed(true);
      return;
    }
    gl.useProgram(prog);

    // One full-viewport triangle strip.
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]),
      gl.STATIC_DRAW,
    );
    const aPos = gl.getAttribLocation(prog, "aPos");
    gl.enableVertexAttribArray(aPos);
    gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);

    const uRes = gl.getUniformLocation(prog, "uRes");
    const uTime = gl.getUniformLocation(prog, "uTime");
    const uLevel = gl.getUniformLocation(prog, "uLevel");
    const uFlow = gl.getUniformLocation(prog, "uFlow");
    const uSpin = gl.getUniformLocation(prog, "uSpin");
    const uPulse = gl.getUniformLocation(prog, "uPulse");

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR);
      const w = Math.max(1, Math.round(canvas.clientWidth * dpr));
      const h = Math.max(1, Math.round(canvas.clientHeight * dpr));
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w;
        canvas.height = h;
      }
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.uniform2f(uRes, canvas.width, canvas.height);
    };
    resize();

    const ro = new ResizeObserver(resize);
    ro.observe(canvas);

    let frame = 0;
    let display = 0;
    let start = 0;
    let prev = 0;
    // Integrated, never recomputed from the clock — see the note in the shader.
    let flowPhase = 0;
    let spinPhase = 0;
    let pulsePhase = 0;
    let lost = false;

    const onLost = (e: Event) => {
      e.preventDefault();
      lost = true;
      setGlFailed(true);
    };
    canvas.addEventListener("webglcontextlost", onLost);

    const tick = (now: number) => {
      if (!start) start = now;
      if (!prev) prev = now;
      const dt = Math.min((now - prev) / 1000, MAX_DT);
      prev = now;

      if (!lost) {
        const raw = getLevelRef.current();
        const floored = speakingRef.current
          ? Math.max(raw * LEVEL_GAIN, SPEAKING_FLOOR)
          : raw * LEVEL_GAIN;
        const target = Math.min(Math.max(floored, 0), 1);
        const tau = target > display ? LEVEL_ATTACK_TAU : LEVEL_RELEASE_TAU;
        display += (target - display) * (1 - Math.exp(-dt / tau));

        flowPhase += dt * (FLOW_BASE + display * FLOW_GAIN);
        spinPhase += dt * (SPIN_BASE + display * SPIN_GAIN);
        pulsePhase += dt * (PULSE_BASE + display * PULSE_GAIN);

        gl.uniform1f(uTime, (now - start) / 1000);
        gl.uniform1f(uLevel, display);
        gl.uniform1f(uFlow, flowPhase);
        gl.uniform1f(uSpin, spinPhase);
        gl.uniform1f(uPulse, pulsePhase);
        gl.clearColor(0, 0, 0, 0);
        gl.clear(gl.COLOR_BUFFER_BIT);
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);

    return () => {
      cancelAnimationFrame(frame);
      ro.disconnect();
      canvas.removeEventListener("webglcontextlost", onLost);
      gl.deleteBuffer(buf);
      gl.deleteProgram(prog);
      gl.deleteShader(vs);
      gl.deleteShader(fs);
    };
  }, []);

  return (
    <div className="agent-orb">
      {glFailed ? (
        <span className="agent-orb__fallback" />
      ) : (
        <canvas ref={canvasRef} className="agent-orb__canvas" aria-hidden />
      )}
    </div>
  );
}
