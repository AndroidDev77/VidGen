import {
  Caption1,
  ProgressBar,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import { WalletCreditCardRegular } from "@fluentui/react-icons";
import type { JSX } from "react";
import type { ProjectCostSummaryResponse } from "@vidgen/contracts";

import { formatMoney } from "../state/format";
import { SectionCard, StatTile, StatTiles, type Tone } from "./Surface";

const useStyles = makeStyles({
  meter: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalXS },
  scroll: { overflowX: "auto" },
  amount: { fontVariantNumeric: "tabular-nums", textAlign: "right" },
  muted: { color: tokens.colorNeutralForeground3 },
  key: { overflowWrap: "anywhere" },
});

export interface CostSummaryProps {
  readonly costs: ProjectCostSummaryResponse;
}

/** Exact decimal amounts against the T23 warning and hard caps. */
export function CostSummary({ costs }: CostSummaryProps): JSX.Element {
  const styles = useStyles();
  const fraction = ratio(costs.committedAmount, costs.hardCap);
  const rows = breakdownRows(costs);
  // The tone tracks the same thresholds the ledger enforces, and the caption
  // below states the position in words so the colour is never the only signal.
  const tone: Tone =
    fraction === null ? "neutral" : fraction >= 0.95 ? "danger" : fraction >= 0.75 ? "warning" : "brand";

  return (
    <SectionCard
      title="Cost"
      icon={<WalletCreditCardRegular />}
      description="Exact decimal amounts, never rounded on the way to the screen."
    >
      <StatTiles>
        <StatTile label="Committed" value={formatMoney(costs.committedAmount)} tone={tone} />
        <StatTile label="Reserved" value={formatMoney(costs.reservedAmount)} />
        <StatTile label="Remaining" value={formatMoney(costs.remainingAmount)} />
        <StatTile label="Warning cap" value={formatMoney(costs.warningCap)} />
        <StatTile label="Hard cap" value={formatMoney(costs.hardCap)} />
      </StatTiles>
      {fraction !== null && (
        <div className={styles.meter}>
          <ProgressBar
            value={fraction}
            max={1}
            shape="rounded"
            thickness="large"
            color={fraction >= 0.95 ? "error" : fraction >= 0.75 ? "warning" : "brand"}
            aria-labelledby="cost-progress-label"
          />
          <Caption1 id="cost-progress-label" className={styles.muted}>
            {formatMoney(costs.committedAmount)} committed of {formatMoney(costs.hardCap)} hard cap
          </Caption1>
        </div>
      )}
      {rows.length > 0 && (
        <div className={styles.scroll}>
          <Table aria-label="Cost by provider, model and operation" size="small">
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Dimension</TableHeaderCell>
                <TableHeaderCell>Key</TableHeaderCell>
                <TableHeaderCell className={styles.amount}>Amount</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => (
                <TableRow key={`${row.dimension}:${row.key}`}>
                  <TableCell className={styles.muted}>{row.dimension}</TableCell>
                  <TableCell className={styles.key}>{row.key}</TableCell>
                  <TableCell className={styles.amount}>{formatMoney(row.amount)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </SectionCard>
  );
}

interface BreakdownRow {
  readonly dimension: string;
  readonly key: string;
  readonly amount: string;
}

function breakdownRows(costs: ProjectCostSummaryResponse): BreakdownRow[] {
  const rows: BreakdownRow[] = [];
  const push = (dimension: string, source: Record<string, string>) => {
    for (const [key, amount] of Object.entries(source)) {
      rows.push({ dimension, key, amount });
    }
  };
  push("Provider", costs.byProvider);
  push("Model", costs.byModel);
  push("Operation", costs.byOperation);
  return rows;
}

/** Compare decimal strings without converting money to a binary float. */
function ratio(committed: string, cap: string): number | null {
  const capValue = Number(cap);
  if (!Number.isFinite(capValue) || capValue <= 0) {
    return null;
  }
  const committedValue = Number(committed);
  return Number.isFinite(committedValue) ? Math.min(committedValue / capValue, 1) : null;
}
