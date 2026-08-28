import { Body1, Caption1, Subtitle2, makeStyles, tokens } from "@fluentui/react-components";
import { useEffect, useState, type JSX } from "react";
import type { VisualQASampleProjection } from "@vidgen/contracts";

import { formatMicroseconds } from "../state/format";

const useStyles = makeStyles({
  wrapper: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalS },
  columns: { display: "flex", gap: tokens.spacingHorizontalM, flexWrap: "wrap" },
  column: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalXS, width: "240px" },
  image: { width: "100%", height: "auto", display: "block" },
});

export interface VisualQAComparisonPanelProps {
  readonly sample: VisualQASampleProjection | null;
  readonly referenceAssetIds: readonly string[];
  readonly resolveAssetUrl: (assetId: string) => Promise<string | null>;
}

/**
 * Side-by-side comparison of one sampled frame and the approved T19 references
 * it was evaluated against, so a reviewer sees what the agent compared.
 */
export function VisualQAComparisonPanel({
  sample,
  referenceAssetIds,
  resolveAssetUrl,
}: VisualQAComparisonPanelProps): JSX.Element {
  const styles = useStyles();
  const [urls, setUrls] = useState<ReadonlyMap<string, string>>(new Map());
  const ids = [
    ...(sample?.frame_asset_id !== null && sample?.frame_asset_id !== undefined
      ? [sample.frame_asset_id]
      : []),
    ...referenceAssetIds,
  ];
  const key = ids.join(",");

  useEffect(() => {
    if (key === "") {
      return;
    }
    let cancelled = false;
    void Promise.all(
      key.split(",").map(async (assetId) => {
        const url = await resolveAssetUrl(assetId);
        return url === null ? null : ([assetId, url] as const);
      }),
    ).then((entries) => {
      if (!cancelled) {
        setUrls(new Map(entries.filter((entry): entry is [string, string] => entry !== null)));
      }
    });
    return () => {
      cancelled = true;
      setUrls(new Map());
    };
  }, [key, resolveAssetUrl]);

  return (
    <section className={styles.wrapper} aria-labelledby="visual-qa-comparison-heading">
      <Subtitle2 as="h3" id="visual-qa-comparison-heading">
        Compared references
      </Subtitle2>
      {sample === null && <Body1>No sampled frame is selected for comparison.</Body1>}
      <div className={styles.columns}>
        {sample !== null && sample.frame_asset_id !== null && (
          <div className={styles.column}>
            <Caption1>Generated at {formatMicroseconds(sample.shot_relative_timestamp_us)}</Caption1>
            {urls.get(sample.frame_asset_id) !== undefined ? (
              <img
                className={styles.image}
                src={urls.get(sample.frame_asset_id)}
                alt={`Sampled frame at ${formatMicroseconds(sample.shot_relative_timestamp_us)}`}
              />
            ) : (
              <Caption1>Frame image unavailable</Caption1>
            )}
          </div>
        )}
        {referenceAssetIds.map((assetId) => (
          <div className={styles.column} key={assetId}>
            <Caption1>Approved T19 reference</Caption1>
            {urls.get(assetId) !== undefined ? (
              <img
                className={styles.image}
                src={urls.get(assetId)}
                alt={`Approved reference ${assetId}`}
              />
            ) : (
              <Caption1>Reference image unavailable</Caption1>
            )}
          </div>
        ))}
        {referenceAssetIds.length === 0 && (
          <Body1>No approved reference was compared for this result.</Body1>
        )}
      </div>
    </section>
  );
}
