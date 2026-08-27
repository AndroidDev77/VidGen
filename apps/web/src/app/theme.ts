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
  20: "#1A0F2E",
  30: "#2A1650",
  40: "#361C68",
  50: "#432380",
  60: "#502A99",
  70: "#5E31B3",
  80: "#6C39CD",
  90: "#7B45E0",
  100: "#8C5AE8",
  110: "#9D70EE",
  120: "#AE86F2",
  130: "#BF9CF6",
  140: "#CFB2F9",
  150: "#DFC9FB",
  160: "#EFE1FD",
};

export const vidgenLightTheme: Theme = createLightTheme(vidgenBrand);
export const vidgenDarkTheme: Theme = createDarkTheme(vidgenBrand);

export type ThemePreference = "light" | "dark";

export const THEME_STORAGE_KEY = "vidgen.theme";
