import { Caption1, Switch, makeStyles, tokens } from "@fluentui/react-components";
import type { JSX } from "react";

const useStyles = makeStyles({
  row: { display: "flex", gap: tokens.spacingHorizontalM, alignItems: "center", flexWrap: "wrap" },
});

export interface CaptionControlsProps {
  readonly enabled: boolean;
  readonly onToggle: (enabled: boolean) => void;
  readonly language: string | null;
  readonly cueCount: number | null;
  readonly subtitleMode: string;
  readonly disabled?: boolean;
}

export function CaptionControls({
  enabled,
  onToggle,
  language,
  cueCount,
  subtitleMode,
  disabled = false,
}: CaptionControlsProps): JSX.Element {
  const styles = useStyles();
  return (
    <div className={styles.row}>
      <Switch
        checked={enabled}
        disabled={disabled}
        label={enabled ? "Captions on" : "Captions off"}
        onChange={(_, data) => onToggle(data.checked)}
        data-testid="caption-toggle"
      />
      <Caption1>
        {subtitleMode === "external" ? "Selectable WebVTT track" : `Subtitle mode: ${subtitleMode}`}
        {language !== null && ` · ${language}`}
        {cueCount !== null && ` · ${cueCount} cues`}
      </Caption1>
    </div>
  );
}
