import {
  Accordion,
  AccordionHeader,
  AccordionItem,
  AccordionPanel,
  Caption1,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import type { JSX } from "react";

import { shortId } from "../state/format";

const useStyles = makeStyles({
  grid: {
    display: "grid",
    gridTemplateColumns: "minmax(140px, max-content) 1fr",
    columnGap: tokens.spacingHorizontalL,
    rowGap: tokens.spacingVerticalXS,
  },
  value: { fontFamily: tokens.fontFamilyMonospace, wordBreak: "break-all" },
});

export interface TechnicalDetailsProps {
  readonly title?: string;
  readonly entries: readonly (readonly [string, string | number | null | undefined])[];
}

/**
 * Compact technical identifiers, collapsed by default.
 *
 * Raw UUIDs and hashes belong here, never as a primary user-facing label.
 */
export function TechnicalDetails({ title, entries }: TechnicalDetailsProps): JSX.Element {
  const styles = useStyles();
  const rows = entries.filter(([, value]) => value !== null && value !== undefined && value !== "");
  return (
    <Accordion collapsible>
      <AccordionItem value="technical">
        <AccordionHeader>{title ?? "Technical details"}</AccordionHeader>
        <AccordionPanel>
          <dl className={styles.grid}>
            {rows.map(([label, value]) => (
              <div key={label} style={{ display: "contents" }}>
                <dt>
                  <Caption1>{label}</Caption1>
                </dt>
                <dd className={styles.value}>
                  <Caption1 title={String(value)}>{compact(String(value))}</Caption1>
                </dd>
              </div>
            ))}
          </dl>
        </AccordionPanel>
      </AccordionItem>
    </Accordion>
  );
}

function compact(value: string): string {
  return /^[0-9a-f-]{32,}$/i.test(value) ? shortId(value) : value;
}
