import { color, font, leading, size } from "../shared/theme/tokens";

export const globalCss = `
  *, *::before, *::after { box-sizing: border-box; }
  html { background: ${color.paper}; }
  body {
    margin: 0;
    color: ${color.ink};
    font-family: ${font.body};
    font-size: ${size.body};
    line-height: ${leading.body};
    -webkit-font-smoothing: antialiased;
  }
  a { color: ${color.ink}; text-underline-offset: 3px; }
  :focus-visible { outline: 3px solid ${color.focus}; outline-offset: 2px; }
  table { border-collapse: collapse; }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
  }
`;
