"use client";

/**
 * Production by subsidiary.
 *
 * The form is a horizontal bar chart in a **single** hue, not eight categorical
 * ones. The reader's job here is "compare magnitude, low to high", and for that
 * job one colour per bar is the right encoding — colouring each bar differently
 * would burn the only free channel restating information the bar length already
 * carries, and a value-ramp would double-encode it. Horizontal because
 * subsidiary names are long enough to collide on a rotated x-axis.
 *
 * A table-view twin sits behind a toggle, so no value is reachable only by
 * hovering.
 */

import { Table2, BarChart3 } from "lucide-react";
import { useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/states";
import { Table, TBody, TD, TH, THead, TR } from "@/components/ui/table";
import { entityLabel, formatTonnes, tickTonnes } from "@/lib/format";
import type { SeriesPoint } from "@/lib/types";

const ROW_HEIGHT = 34;
const AXIS_BAND = 52;

interface Row {
  entity: string;
  value: number;
  factCount: number;
  display: string;
  exact: string;
}

export function ProductionChart({
  points,
  highlightEntity,
  unitLabel = "tonnes",
}: {
  points: SeriesPoint[];
  /** Renders every other bar recessive. Used when one subsidiary is the story. */
  highlightEntity?: string | null;
  unitLabel?: string;
}) {
  const [view, setView] = useState<"chart" | "table">("chart");

  const rows = useMemo<Row[]>(
    () =>
      [...points]
        .sort((a, b) => a.value - b.value)
        .map((point) => {
          const { display, exact } = formatTonnes(point.value);
          return {
            entity: entityLabel(point.entity_id),
            value: point.value,
            factCount: point.fact_count,
            display,
            exact,
          };
        }),
    [points],
  );

  if (rows.length === 0) {
    return (
      <EmptyState icon={BarChart3} title="No fiscal-year figures for this metric">
        Calendar-year facts are excluded from this axis on purpose — charting them
        beside fiscal-year figures is the comparison the period normalizer refuses
        to make.
      </EmptyState>
    );
  }

  return (
    <div>
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="text-xs text-ink-3">
          {rows.length} {rows.length === 1 ? "entity" : "entities"} · sorted by volume
        </p>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => setView(view === "chart" ? "table" : "chart")}
          aria-pressed={view === "table"}
        >
          {view === "chart" ? (
            <>
              <Table2 className="size-3.5" aria-hidden /> Table view
            </>
          ) : (
            <>
              <BarChart3 className="size-3.5" aria-hidden /> Chart view
            </>
          )}
        </Button>
      </div>

      {view === "table" ? (
        <Table>
          <THead>
            <TR>
              <TH>Entity</TH>
              <TH numeric>Production</TH>
              <TH numeric>Exact ({unitLabel})</TH>
              <TH numeric>Facts</TH>
            </TR>
          </THead>
          <TBody>
            {[...rows].reverse().map((row) => (
              <TR key={row.entity}>
                <TD className="font-medium">{row.entity}</TD>
                <TD numeric>{row.display}</TD>
                <TD numeric className="text-ink-2">
                  {row.exact}
                </TD>
                <TD numeric className="text-ink-2">
                  {row.factCount}
                </TD>
              </TR>
            ))}
          </TBody>
        </Table>
      ) : (
        <ResponsiveContainer
          width="100%"
          height={rows.length * ROW_HEIGHT + AXIS_BAND}
        >
          <BarChart
            data={rows}
            layout="vertical"
            margin={{ top: 4, right: 68, bottom: 4, left: 4 }}
            barCategoryGap="28%"
          >
            <CartesianGrid horizontal={false} stroke="var(--color-hairline)" />
            <XAxis
              type="number"
              tickFormatter={tickTonnes}
              tick={{ fill: "var(--color-ink-3)", fontSize: 11 }}
              tickLine={false}
              axisLine={{ stroke: "var(--color-rule)" }}
              label={{
                value: `Production (${unitLabel})`,
                position: "insideBottom",
                offset: -2,
                fill: "var(--color-ink-3)",
                fontSize: 11,
              }}
            />
            <YAxis
              type="category"
              dataKey="entity"
              width={64}
              tick={{ fill: "var(--color-ink-2)", fontSize: 12 }}
              tickLine={false}
              axisLine={{ stroke: "var(--color-rule)" }}
            />
            <Tooltip
              cursor={{ fill: "var(--color-plane)" }}
              content={<ProductionTooltip />}
            />
            <Bar dataKey="value" radius={[0, 4, 4, 0]} maxBarSize={22}>
              {rows.map((row) => (
                <Cell
                  key={row.entity}
                  fill={
                    highlightEntity && entityLabel(highlightEntity) !== row.entity
                      ? "var(--color-blue-100)"
                      : "var(--color-series-1)"
                  }
                />
              ))}
              <LabelList
                dataKey="display"
                position="right"
                offset={8}
                className="tnum"
                fill="var(--color-ink-2)"
                fontSize={11}
              />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}

function ProductionTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: { payload: Row }[];
}) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload;

  return (
    <div className="rounded-md border border-hairline bg-surface px-3 py-2 text-xs shadow-sm">
      <p className="font-semibold text-ink">{row.entity}</p>
      <p className="tnum mt-1 text-ink">{row.display}</p>
      <p className="tnum text-ink-3">{row.exact}</p>
      <p className="mt-1 text-ink-2">
        from {row.factCount} {row.factCount === 1 ? "fact" : "facts"}
      </p>
    </div>
  );
}
