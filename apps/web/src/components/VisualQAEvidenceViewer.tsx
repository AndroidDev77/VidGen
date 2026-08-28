import {
  Body1,
  Button,
  Caption1,
  Subtitle2,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { useEffect, useState, type JSX } from "react";
import type {
  VisualQAEvidenceProjection,
  VisualQASampleProjection,
} from "@vidgen/contracts";

import { formatMicroseconds } from "../state/format";

const useStyles = makeStyles({
  wrapper: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalS },
  item: {
    display: "flex",
    gap: tokens.spacingHorizontalM,
    flexWrap: "wrap",
    padding: tokens.spacingVerticalS,
    border: `1px solid ${tokens.colorNeutralStroke2}`,
    borderRadius: tokens.borderRadiusMedium,
  },
  frame: { position: "relative", width: "240px", maxWidth: "100%" },
  image: { width: "100%", height: "auto", display: "block" },
  box: {
    position: "absolute",
    border: `2px solid ${tokens.colorPaletteRedBorderActive}`,
    pointerEvents: "none",
  },
  meta: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalXS, minWidth: "220px" },
});

export interface VisualQAEvidenceViewerProps {
  readonly evidence: readonly VisualQAEvidenceProjection[];
  readonly samples: readonly VisualQASampleProjection[];
  /** Resolves an asset ID to a short-lived signed URL, requested just before use. */
  readonly resolveAssetUrl: (assetId: string) => Promise<string | null>;
}

/**
 * Displays each evidence frame at its exact timestamp beside the approved
 * reference it was compared against.
 *
 * Signed URLs are resolved on demand and held only in component state, so an
 * expired URL is never rendered: "Refresh frames" requests new ones.
 */
export function VisualQAEvidenceViewer({
  evidence,
  samples,
  resolveAssetUrl,
}: VisualQAEvidenceViewerProps): JSX.Element {
  const styles = useStyles();
  const [urls, setUrls] = useState<ReadonlyMap<string, string>>(new Map());
  const [generation, setGeneration] = useState(0);
  const assetIds = Array.from(
    new Set(
      evidence.flatMap((item) =>
        [item.frame_asset_id, item.compared_reference_asset_id].filter(
          (value): value is string => value !== null,
        ),
      ),
    ),
  ).sort();
  const assetKey = assetIds.join(",");

  useEffect(() => {
    if (assetKey === "") {
      return;
    }
    let cancelled = false;
    void Promise.all(
      assetKey.split(",").map(async (assetId) => {
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
      // Drop the signed URLs as soon as this view goes away.
      setUrls(new Map());
    };
  }, [assetKey, resolveAssetUrl, generation]);

  const timestampFor = (item: VisualQAEvidenceProjection): number =>
    item.shot_relative_timestamp_us ?? item.source_relative_timestamp_us ?? 0;
  const sampleFor = (item: VisualQAEvidenceProjection): VisualQASampleProjection | undefined =>
    samples.find((sample) => sample.sample_id === item.sample_id);

  return (
    <section className={styles.wrapper} aria-labelledby="visual-qa-evidence-heading">
      <Subtitle2 as="h3" id="visual-qa-evidence-heading">
        Evidence frames
      </Subtitle2>
      <Button size="small" appearance="secondary" onClick={() => setGeneration((n) => n + 1)}>
        Refresh frames
      </Button>
      {evidence.length === 0 && <Body1>This result cites no evidence frames.</Body1>}
      {evidence.map((item) => {
        const frameUrl = item.frame_asset_id === null ? null : urls.get(item.frame_asset_id);
        const referenceUrl =
          item.compared_reference_asset_id === null
            ? null
            : urls.get(item.compared_reference_asset_id);
        const sample = sampleFor(item);
        const label = `Evidence for finding ${item.finding_id} at ${formatMicroseconds(
          timestampFor(item),
        )}`;
        return (
          <div className={styles.item} key={item.evidence_id}>
            <div className={styles.frame}>
              {frameUrl !== undefined && frameUrl !== null ? (
                <>
                  <img className={styles.image} src={frameUrl} alt={label} />
                  {item.bounding_box !== null && (
                    <span
                      className={styles.box}
                      data-testid={`bounding-box-${item.evidence_id}`}
                      style={{
                        left: `${item.bounding_box.x * 100}%`,
                        top: `${item.bounding_box.y * 100}%`,
                        width: `${item.bounding_box.width * 100}%`,
                        height: `${item.bounding_box.height * 100}%`,
                      }}
                    />
                  )}
                </>
              ) : (
                <Caption1>Frame image unavailable</Caption1>
              )}
            </div>
            <div className={styles.meta}>
              <Caption1>Timestamp {formatMicroseconds(timestampFor(item))}</Caption1>
              <Caption1>Supports finding {item.finding_id}</Caption1>
              {sample !== undefined && <Caption1>Sampled because: {sample.selection_reason}</Caption1>}
              <Caption1>Confidence {item.confidence.toFixed(2)}</Caption1>
              {item.explanation !== "" && <Body1>{item.explanation}</Body1>}
            </div>
            {referenceUrl !== undefined && referenceUrl !== null && (
              <div className={styles.frame}>
                <img
                  className={styles.image}
                  src={referenceUrl}
                  alt={`Approved reference compared against finding ${item.finding_id}`}
                />
                <Caption1>Approved T19 reference</Caption1>
              </div>
            )}
          </div>
        );
      })}
    </section>
  );
}
