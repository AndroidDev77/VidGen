import {
  Caption1,
  ProgressBar,
  Subtitle2,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  makeStyles,
  tokens,
} from "@fluentui/react-components";
import type { JSX } from "react";
import type { ProjectCostSummaryResponse } from "@vidgen/contracts";

import { formatMoney } from "../state/format";

const useStyles = makeStyles({
  wrapper: { display: "flex", flexDirection: "column", gap: tokens.spacingVerticalM },
  totals: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))",
    gap: tokens.spacingHorizontalM,
  },
  total: { display: "flex", flexDirection: "column" },
  scroll: { overflowX: "auto" },
  amount: { fontVariantNumeric: "tabular-nums" },
});

export interface CostSummaryProps {
  readonly costs: ProjectCostSummaryResponse;
}

/** Exact decimal amounts against the T23 warning and hard caps. */
export function CostSummary({ costs }: CostSummaryProps): JSX.Element {
  const styles = useStyles();
  const fraction = ratio(costs.committedAmount, costs.hardCap);
  return (
    <section className={styles.wrapper} aria-labelledby="cost-summary-heading">
      <Subtitle2 as="h2" id="cost-summary-heading">
        Cost
      </Subtitle2>
      <div className={styles.totals}>
        <Total label="Committed" amount={costs.committedAmount} />
        <Total label="Reserved" amount={costs.reservedAmount} />
        <Total label="Remaining" amount={costs.remainingAmount} />
        <Total label="Warning cap" amount={costs.warningCap} />
        <Total label="Hard cap" amount={costs.hardCap} />
      </div>
      {fraction !== null && (
        <>
          <ProgressBar value={fraction} max={1} aria-labelledby="cost-progress-label" />
          <Caption1 id="cost-progress-label">
            {formatMoney(costs.committedAmount)} committed of {formatMoney(costs.hardCap)} hard cap
          </Caption1>
        </>
      )}
      <div className={styles.scroll}>
        <Table aria-label="Cost by provider, model and operation" size="small">
          <TableHeader>
            <TableRow>
              <TableHeaderCell>Dimension</TableHeaderCell>
              <TableHeaderCell>Key</TableHeaderCell>
              <TableHeaderCell>Amount</TableHeaderCell>
            </TableRow>
          </TableHeader>
          <TableBody>
            {breakdownRows(costs).map((row) => (
              <TableRow key={`${row.dimension}:${row.key}`}>
                <TableCell>{row.dimension}</TableCell>
                <TableCell>{row.key}</TableCell>
                <TableCell className={styles.amount}>{formatMoney(row.amount)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </section>
  );
}

function Total({ label, amount }: { readonly label: string; readonly amount: string }): JSX.Element {
  const styles = useStyles();
  return (
    <div className={styles.total}>
      <Caption1>{label}</Caption1>
      <Subtitle2 className={styles.amount}>{formatMoney(amount)}</Subtitle2>
    </div>
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
