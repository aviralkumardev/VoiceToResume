
export const ORB_VERT = `
attribute vec2 aPos;
void main() {
  gl_Position = vec4(aPos, 0.0, 1.0);
}
`;

export const ORB_FRAG = `
precision highp float;

uniform vec2  uRes;
uniform float uTime;
uniform float uLevel;
/* Accumulated phases, integrated on the JS side to avoid phase jumps. */
uniform float uFlow;
uniform float uSpin;
uniform float uPulse;

/* Outer glow and wave travel are both held inside this radius, in uv units. */
const float GLOW_MAX = 0.200;

/* ---- Gradient & Perlin-style noise helpers ----------------------------- */
vec2 hash2(vec2 p) {
  p = vec2(dot(p, vec2(127.1, 311.7)), dot(p, vec2(269.5, 183.3)));
  return -1.0 + 2.0 * fract(sin(p) * 43758.5453123);
}

float gnoise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(
    mix(dot(hash2(i + vec2(0.0, 0.0)), f - vec2(0.0, 0.0)),
        dot(hash2(i + vec2(1.0, 0.0)), f - vec2(1.0, 0.0)), u.x),
    mix(dot(hash2(i + vec2(0.0, 1.0)), f - vec2(0.0, 1.0)),
        dot(hash2(i + vec2(1.0, 1.0)), f - vec2(1.0, 1.0)), u.x),
    u.y);
}

/* Fast 3-octave rotated fBm for atmospheric organic curtain waves */
float fbm(vec2 p) {
  float v = 0.0;
  float a = 0.55;
  mat2 rot = mat2(0.80, 0.60, -0.60, 0.80);
  for (int i = 0; i < 3; i++) {
    v += a * gnoise(p);
    p = rot * p * 2.15;
    a *= 0.5;
  }
  return v;
}

/* Interleaved gradient noise for silky dithering without banding */
float ign(vec2 p) {
  return fract(52.9829189 * fract(0.06711056 * p.x + 0.00583715 * p.y));
}

/* Aurora curtain ribbon: computes the physical light distribution and color of
   an undulating curtain fold with vertical ray columns streaming along
   geomagnetic field lines. */
vec3 auroraCurtain(
  vec2 p,
  float yOffset,
  float waveSpeed,
  float rayScale,
  float flow,
  float lvl,
  vec3 baseCol,
  vec3 midCol,
  vec3 topCol
) {
  /* Undulating sinusoidal + noise ribbon base */
  float wave = sin(p.x * 2.1 + flow * 1.4 + waveSpeed) * 0.19
             + sin(p.x * 4.2 - flow * 0.8 + yOffset) * 0.09
             + fbm(vec2(p.x * 1.1 + flow * 0.35, yOffset)) * 0.15;

  float dy = p.y - (yOffset + wave);
  /* Both bounds sit exactly where the profile below is already zero: the lower
     one at the foot of the smoothstep, the upper one at the end of tipFade.
     Cutting anywhere the curtain still has brightness (the old dy > 0.90 was
     still at ~0.15) draws a straight hard line across the disc. */
  if (dy < -0.10 || dy > 1.06) return vec3(0.0);

  /* Vertical light rays (geomagnetic striations). The pitch and the sharpening
     exponent are a legibility-vs-shimmer trade: at 1x DPR the bright band of a
     pow-2.2 ray at pitch 5.5 lands around 2px and crawls as the field drifts. */
  float rayNoise = fbm(vec2(p.x * rayScale + flow * 0.5, p.y * 0.4));
  float rayPattern = sin(p.x * (rayScale * 4.8) + rayNoise * 4.5 + sin(p.y * 7.0 + flow)) * 0.5 + 0.5;
  rayPattern = pow(rayPattern, 2.0) * 1.7 + 0.25;

  /* Intensity profile: crisp lower boundary with upward streaming ray falloff.
     tipFade carries the exponential tail the rest of the way to zero so the
     early-out above is a no-op rather than a cut. */
  float tipFade = 1.0 - smoothstep(0.52, 1.06, dy);
  float baseGlow = smoothstep(-0.10, 0.03, dy) * exp(-dy * 2.6) * tipFade;
  float rayIntensity = baseGlow * (0.35 + 0.65 * rayPattern);

  /* Color gradation along the curtain: Electric Blue -> Violet -> Magenta */
  float t = clamp(dy * 1.7, 0.0, 1.0);
  vec3 col = mix(baseCol, midCol, smoothstep(0.0, 0.42, t));
  col = mix(col, topCol, smoothstep(0.35, 0.92, t));

  /* Fold crest highlights that ignite on speech */
  float crestHighlight = pow(clamp(1.0 - abs(dy - 0.015) * 5.5, 0.0, 1.0), 3.0);
  col += vec3(0.88, 0.92, 1.0) * (crestHighlight * (0.35 + lvl * 0.60));

  return col * rayIntensity;
}

void main() {
  vec2 uv = (gl_FragCoord.xy * 2.0 - uRes) / uRes.y;

  float lvl = clamp(uLevel, 0.0, 1.0);
  float r = length(uv);
  float ang = atan(uv.y, uv.x);

  /* Angular breathing harmonics */
  float voice = sin(ang * 3.0 - uTime * 0.95) * 0.55
              + sin(ang * 5.0 + uTime * 0.70) * 0.30
              + sin(ang * 8.0 - uTime * 1.25) * 0.16;

  float breathe = 0.010 * sin(uTime * 0.80);
  float R = 0.530 + breathe + lvl * 0.045 + lvl * 0.012 * voice;

  /* Crisp disc perimeter */
  float aa = 2.4 / uRes.y;
  float body = smoothstep(R + aa, R - aa, r);

  /* ---- Outside halo and pulse waves ----------------------------------- */
  float d0 = max(r - R, 0.0);
  float contain = 1.0 - smoothstep(0.0, GLOW_MAX, d0);
  float glowTight = (1.0 - smoothstep(0.0, 0.075, d0)) * 0.12;
  float glowBroad = contain * lvl * 0.36;

  float waves = 0.0;
  for (int i = 0; i < 2; i++) {
    float f = fract(uPulse - float(i) * 0.5);
    float rr = R - 0.02 + f * 0.17;
    float wd = 0.045 * (0.6 + f);
    float g = (r - rr) / wd;
    waves += exp(-g * g) * (1.0 - f) * (1.0 - f);
  }
  waves *= lvl * 0.24 * contain;

  /* Ambient ethereal halo. Level enters as an OFFSET on the hue mix, not a
     rate, so speech leans the surrounding air toward the curtains own violet
     without touching the phase integrators. */
  float hHalo = clamp(0.5 + 0.35 * sin(uTime * 0.10) + lvl * 0.22, 0.0, 1.0);
  vec3 haloCol = mix(vec3(0.12, 0.35, 0.95), vec3(0.55, 0.18, 0.92), hHalo);

  vec3 col = haloCol;

  /* ---- Inside the disc: Aurora Borealis Curtains & Rays ---------------- */
  if (r < R + aa) {
    /* Cosmic night sky backdrop */
    vec3 bgDeep = vec3(0.04, 0.06, 0.22);
    vec3 bgViolet = vec3(0.08, 0.04, 0.27);
    /* The drift is at a CONSTANT rate (never level-dependent). Without it the
       backdrop blotches are frozen while everything in front of them moves. */
    vec2 skyDrift = vec2(uTime * 0.013, uTime * -0.009);
    vec3 sky = mix(bgDeep, bgViolet, clamp(uv.y * 0.6 + 0.5 + fbm(uv * 0.8 + skyDrift) * 0.2, 0.0, 1.0));

    /* Organic diagonal orientation for northern lights flow */
    float tilt = -0.32;
    mat2 rot = mat2(cos(tilt), -sin(tilt), sin(tilt), cos(tilt));
    vec2 ap = rot * uv;

    /* Subtle vortex shear */
    float shear = (1.0 - smoothstep(0.0, 0.85, r)) * (0.35 + lvl * 0.16);
    float sa = uSpin * 0.8 + shear;
    mat2 rotShear = mat2(cos(sa), -sin(sa), sin(sa), cos(sa));
    ap = rotShear * ap;

    /* 3 Layered Aurora Curtains */
    /* Curtain 1: Background deep curtain (ethereal violet-magenta rays) */
    vec3 c1 = auroraCurtain(
      ap * 1.15,
      0.18,
      1.1,
      2.1,
      uFlow * 0.85,
      lvl,
      vec3(0.15, 0.30, 0.92),
      vec3(0.58, 0.18, 0.92),
      vec3(0.86, 0.30, 0.96)
    );

    /* Curtain 2: Primary vibrant curtain (bright electric blue -> rich violet) */
    vec3 c2 = auroraCurtain(
      ap * 1.0,
      -0.10,
      2.7,
      2.9,
      uFlow * 1.15,
      lvl,
      vec3(0.06, 0.56, 1.00),
      vec3(0.40, 0.22, 0.95),
      vec3(0.78, 0.26, 0.96)
    );

    /* Curtain 3: Foreground luminous ribbon (cyan-blue leading edge -> lavender) */
    vec3 c3 = auroraCurtain(
      ap * 1.25,
      -0.36,
      4.2,
      3.6,
      uFlow * 1.4,
      lvl,
      vec3(0.14, 0.68, 0.98),
      vec3(0.48, 0.28, 0.96),
      vec3(0.92, 0.72, 1.00)
    );

    /* Composite curtains over dark sky */
    vec3 aurora = sky + c1 * 0.90 + c2 * 1.20 + c3 * 1.00;

    /* Ethereal speech shimmer */
    aurora += vec3(0.75, 0.55, 1.0) * (lvl * 0.16 * (c2.r + c3.b));

    /* Soft filmic roll-off. Three curtains plus their crest highlights stack
       well past 1.0 where folds overlap (measured ~3.4 in a lit crest), and
       letting the framebuffer clip that flattens the brightest masses into
       featureless white blobs with hard borders, the single biggest source of
       coarseness in the aurora. The 1.35 exposure holds mid-tones roughly where
       they were while compressing only the peaks, so this softens without
       dimming. Also lifts the deepest sky slightly, which helps the disc sit in
       its halo instead of ringing against it. */
    col = vec3(1.0) - exp(-max(aurora, 0.0) * 1.35);
  }

  vec3 rgb = mix(haloCol, col, body);
  float alpha = clamp(body + glowTight + glowBroad + waves, 0.0, 1.0);

  /* Ordered dither on colour and alpha to eliminate quantization banding */
  float dth = (ign(gl_FragCoord.xy) - 0.5) / 255.0;
  rgb += dth;
  alpha += dth * mix(2.0, 1.0, body);

  gl_FragColor = vec4(rgb, alpha);
}
`;
