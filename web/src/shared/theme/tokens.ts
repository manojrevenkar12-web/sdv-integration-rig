// Design tokens. Components read every colour, size, space and duration from here.
// Palette: a harness drawing — drafting paper, navy ink, and the wire colours
// automotive harnesses use (CAN-H yellow, CAN-L green, high-voltage orange).

export const color = {
  paper: "#EDF0F2",
  sheet: "#F7F8F9",
  ink: "#18233A",
  graphite: "#5A6577",
  rule: "#C9D0D8",
  grid: "#DCE1E6",
  canH: "#C99400",
  canL: "#2F7D57",
  hv: "#D35A1F",
  shield: "#7C8DA6",
  focus: "#2F5FD0",
} as const;

export const font = {
  body: "'Barlow', system-ui, sans-serif",
  figures: "'Barlow Semi Condensed', 'Barlow', system-ui, sans-serif",
} as const;

// Type scale: a 1.25 ratio from a 17px body.
export const size = {
  small: "0.8125rem",
  body: "1.0625rem",
  lead: "1.3125rem",
  h2: "1.625rem",
  h1: "2.5rem",
  figure: "2rem",
} as const;

export const weight = { regular: 400, medium: 500, semibold: 600 } as const;
export const leading = { tight: 1.15, body: 1.5 } as const;

export const space = { xs: 4, sm: 8, md: 16, lg: 24, xl: 40, xxl: 72 } as const;
export const radius = { control: 6, panel: 2 } as const;
export const measure = { page: 1040, text: 680 } as const;
export const stroke = { hair: 1, wire: 3, lane: 18 } as const;

export const motion = {
  drawSeconds: 1.6,
  quickSeconds: 0.25,
  ease: [0.22, 1, 0.36, 1] as const,
} as const;

export const breakpoint = { narrow: 720 } as const;

// Text sizes inside the SVG trace, in viewBox units.
export const svgText = { label: 15, note: 13, tick: 12 } as const;

// The pass meter: one tick per scenario.
export const meter = { tick: 6, height: 14, gap: 2 } as const;
