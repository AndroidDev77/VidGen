import {
  createDarkTheme,
  createLightTheme,
  type BrandVariants,
  type Theme,
} from "@fluentui/react-components";

/**
 * A production-desk palette: deep indigo rather than the Fluent default blue,
 * so the app reads as a media tool instead of a generic admin console.
 */
const vidgenBrand: BrandVariants = {
  10: "#04010B",
  20: "#170C2A",
  30: "#26134A",
  40: "#331862",
  50: "#401E7A",
  60: "#4E2594",
  70: "#5C2DAE",
  80: "#6B37C8",
  90: "#7A44DC",
  100: "#8B59E6",
  110: "#9C6FEC",
  120: "#AD85F1",
  130: "#BE9BF5",
  140: "#CEB2F8",
  150: "#DFC9FB",
  160: "#EFE1FD",
};

/**
 * The typeface stack.
 *
 * Every family here is either bundled with a mainstream OS or already
 * installed by the people who use those OSes, so the shell paints its final
 * type on the first frame with no webfont request, no layout shift, and no
 * dependency on a font CDN a self-hosted deployment may not be able to reach.
 */
const FONT_FAMILY_BASE = [
  "Inter",
  '"Inter Variable"',
  '"Segoe UI Variable Text"',
  '"Segoe UI"',
  "system-ui",
  "-apple-system",
  "BlinkMacSystemFont",
  '"Helvetica Neue"',
  "Arial",
  "sans-serif",
].join(", ");

const FONT_FAMILY_MONOSPACE = [
  '"JetBrains Mono"',
  '"SF Mono"',
  '"Cascadia Mono"',
  "ui-monospace",
  "SFMono-Regular",
  "Menlo",
  "Consolas",
  "monospace",
].join(", ");

/**
 * Softer, more diffuse elevation than the Fluent defaults.
 *
 * The stock shadows are tuned for dense Office chrome; a workflow console
 * carries a handful of large surfaces instead, and those read better with a
 * wide, low-opacity ambient shadow over a tight contact shadow.
 */
const lightShadows = {
  shadow2: "0 1px 2px rgba(16, 8, 34, 0.06), 0 1px 3px rgba(16, 8, 34, 0.04)",
  shadow4: "0 2px 4px rgba(16, 8, 34, 0.06), 0 4px 12px rgba(16, 8, 34, 0.05)",
  shadow8: "0 4px 8px rgba(16, 8, 34, 0.07), 0 8px 24px rgba(16, 8, 34, 0.07)",
  shadow16: "0 8px 16px rgba(16, 8, 34, 0.08), 0 16px 40px rgba(16, 8, 34, 0.09)",
  shadow28: "0 12px 24px rgba(16, 8, 34, 0.1), 0 28px 64px rgba(16, 8, 34, 0.12)",
  shadow64: "0 24px 48px rgba(16, 8, 34, 0.12), 0 48px 96px rgba(16, 8, 34, 0.16)",
};

/** Dark surfaces sit on a near-black ground, so the shadow is depth, not haze. */
const darkShadows = {
  shadow2: "0 1px 2px rgba(0, 0, 0, 0.4)",
  shadow4: "0 2px 4px rgba(0, 0, 0, 0.42), 0 4px 12px rgba(0, 0, 0, 0.32)",
  shadow8: "0 4px 8px rgba(0, 0, 0, 0.45), 0 8px 24px rgba(0, 0, 0, 0.36)",
  shadow16: "0 8px 16px rgba(0, 0, 0, 0.5), 0 16px 40px rgba(0, 0, 0, 0.4)",
  shadow28: "0 12px 24px rgba(0, 0, 0, 0.55), 0 28px 64px rgba(0, 0, 0, 0.45)",
  shadow64: "0 24px 48px rgba(0, 0, 0, 0.6), 0 48px 96px rgba(0, 0, 0, 0.5)",
};

/**
 * Rounder corners than the Fluent defaults (2/4/6/8px).
 *
 * The scale is kept proportional so a control nested inside a card still reads
 * as concentric rather than as a mismatched pill.
 */
const radii = {
  borderRadiusSmall: "4px",
  borderRadiusMedium: "8px",
  borderRadiusLarge: "12px",
  borderRadiusXLarge: "16px",
};

const typography = {
  fontFamilyBase: FONT_FAMILY_BASE,
  fontFamilyMonospace: FONT_FAMILY_MONOSPACE,
  fontFamilyNumeric: FONT_FAMILY_BASE,
};

const baseLight = createLightTheme(vidgenBrand);
const baseDark = createDarkTheme(vidgenBrand);

export const vidgenLightTheme: Theme = {
  ...baseLight,
  ...typography,
  ...radii,
  ...lightShadows,
  // A faint violet cast in the neutrals ties the greys to the brand instead of
  // leaving the page a pure, clinical grey.
  colorNeutralBackground1: "#FFFFFF",
  colorNeutralBackground1Hover: "#F7F5FB",
  colorNeutralBackground1Pressed: "#EFECF6",
  colorNeutralBackground1Selected: "#F2EFF9",
  colorNeutralBackground2: "#F6F4FA",
  colorNeutralBackground3: "#F0EDF7",
  colorNeutralBackground4: "#EAE6F3",
  colorNeutralStroke1: "#DFDAEA",
  colorNeutralStroke2: "#E8E4F1",
  colorNeutralStroke3: "#F1EEF7",
  colorNeutralStrokeSubtle: "#EDEAF4",
};

export const vidgenDarkTheme: Theme = {
  ...baseDark,
  ...typography,
  ...radii,
  ...darkShadows,
  // Dark mode is a graded stack of tinted near-blacks rather than one flat
  // grey, so a card, the page behind it and a nested row stay distinguishable
  // without needing a border on every edge.
  colorNeutralBackground1: "#17141F",
  colorNeutralBackground1Hover: "#1F1B2A",
  colorNeutralBackground1Pressed: "#241F31",
  colorNeutralBackground1Selected: "#221D2E",
  colorNeutralBackground2: "#100E16",
  colorNeutralBackground3: "#1C1826",
  colorNeutralBackground4: "#13111A",
  colorNeutralBackground5: "#0B0910",
  colorNeutralBackground6: "#221D2E",
  colorNeutralStroke1: "#332C44",
  colorNeutralStroke2: "#282235",
  colorNeutralStroke3: "#211C2C",
  colorNeutralStrokeSubtle: "#282235",
};

export type ThemePreference = "light" | "dark";

export const THEME_STORAGE_KEY = "vidgen.theme";
