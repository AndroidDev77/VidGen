import { Body1, Caption1, makeStyles, tokens } from "@fluentui/react-components";
import { useEffect, useRef, type JSX } from "react";

const useStyles = makeStyles({
  wrapper: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalS },
  video: {
    width: "100%",
    maxHeight: "60vh",
    borderRadius: tokens.borderRadiusMedium,
    backgroundColor: tokens.colorNeutralBackground3,
  },
});

export interface VideoPreviewProps {
  readonly videoUrl: string | null;
  readonly captionsUrl: string | null;
  readonly captionsEnabled: boolean;
  readonly captionLanguage: string;
  readonly title: string;
}

/**
 * The final render, with browser-native selectable captions.
 *
 * The standalone WebVTT asset is loaded into a `<track>` element rather than
 * burned in, so the viewer can turn captions on and off natively. Both URLs are
 * short-lived signed URLs supplied by the caller just before playback.
 */
export function VideoPreview({
  videoUrl,
  captionsUrl,
  captionsEnabled,
  captionLanguage,
  title,
}: VideoPreviewProps): JSX.Element {
  const styles = useStyles();
  const videoRef = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    const element = videoRef.current;
    if (!element) {
      return;
    }
    const tracks = element.textTracks;
    for (let index = 0; index < tracks.length; index += 1) {
      const track = tracks[index];
      if (track) {
        track.mode = captionsEnabled ? "showing" : "disabled";
      }
    }
  }, [captionsEnabled, captionsUrl]);

  if (videoUrl === null) {
    return (
      <div className={styles.wrapper}>
        <Body1>The final video is not available for playback yet.</Body1>
      </div>
    );
  }

  return (
    <div className={styles.wrapper}>
      {/* eslint-disable-next-line jsx-a11y/media-has-caption -- a <track> is rendered below when captions exist */}
      <video
        ref={videoRef}
        className={styles.video}
        controls
        preload="metadata"
        crossOrigin="anonymous"
        aria-label={`Final render preview for ${title}`}
        data-testid="final-render-video"
      >
        <source src={videoUrl} type="video/mp4" />
        {captionsUrl !== null && (
          <track
            kind="captions"
            src={captionsUrl}
            srcLang={captionLanguage}
            label={`Captions (${captionLanguage})`}
            default={captionsEnabled}
            data-testid="final-render-captions"
          />
        )}
        Your browser cannot play this video.
      </video>
      {captionsUrl === null && <Caption1>No caption track is attached to this render.</Caption1>}
    </div>
  );
}
