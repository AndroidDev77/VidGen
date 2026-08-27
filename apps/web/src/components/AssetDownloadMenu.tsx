import {
  Button,
  Menu,
  MenuItem,
  MenuList,
  MenuPopover,
  MenuTrigger,
  Spinner,
} from "@fluentui/react-components";
import { ArrowDownloadRegular } from "@fluentui/react-icons";
import { useCallback, useState, type JSX } from "react";

import { apiClient, type VidGenClient } from "../api/client";
import { getDownloadUrl } from "../api/uploads";

export interface DownloadTarget {
  readonly label: string;
  readonly assetId: string | null;
  readonly fileName: string;
}

export interface AssetDownloadMenuProps {
  readonly targets: readonly DownloadTarget[];
  readonly client?: VidGenClient;
  readonly onError?: (error: unknown) => void;
}

/**
 * Request a signed download URL immediately before use.
 *
 * The URL is never stored in application state, the query cache, or browser
 * storage: it is fetched, used once to open the asset, and discarded.
 */
export function AssetDownloadMenu({
  targets,
  client = apiClient,
  onError,
}: AssetDownloadMenuProps): JSX.Element {
  const [busy, setBusy] = useState<string | null>(null);

  const download = useCallback(
    async (target: DownloadTarget) => {
      if (target.assetId === null) {
        return;
      }
      setBusy(target.assetId);
      try {
        const { data } = await getDownloadUrl(target.assetId, client);
        const anchor = document.createElement("a");
        anchor.href = data.url;
        anchor.download = target.fileName;
        anchor.rel = "noopener";
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
      } catch (error) {
        onError?.(error);
      } finally {
        setBusy(null);
      }
    },
    [client, onError],
  );

  return (
    <Menu>
      <MenuTrigger disableButtonEnhancement>
        <Button appearance="primary" icon={<ArrowDownloadRegular />}>
          Download
        </Button>
      </MenuTrigger>
      <MenuPopover>
        <MenuList>
          {targets.map((target) => (
            <MenuItem
              key={target.label}
              disabled={target.assetId === null || busy !== null}
              icon={busy === target.assetId ? <Spinner size="tiny" /> : undefined}
              onClick={() => void download(target)}
            >
              {target.label}
              {target.assetId === null ? " (not available)" : ""}
            </MenuItem>
          ))}
        </MenuList>
      </MenuPopover>
    </Menu>
  );
}
