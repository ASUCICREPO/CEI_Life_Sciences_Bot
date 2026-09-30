'use client';

import { useMemo, useState } from 'react';
import type { TableCell, TableComponent } from '@/lib/cei-ui/schema';

type SortState = { columnIndex: number; direction: 'ascending' | 'descending' };

export function TableView({ component }: { component: TableComponent }) {
  const [sort, setSort] = useState<SortState | null>(null);
  const flaggedCells = useMemo(
    () => new Set(component.flagged_cells.map((cell) => `${cell.row}:${cell.column}`)),
    [component.flagged_cells],
  );
  const indexedRows = useMemo(
    () => component.rows.map((row, rowIndex) => ({ row, rowIndex })),
    [component.rows],
  );
  const sortedRows = useMemo(() => {
    if (!component.sortable || !sort) return indexedRows;
    const { columnIndex, direction } = sort;
    const factor = direction === 'ascending' ? 1 : -1;
    return [...indexedRows].sort((left, right) => compareCells(left.row[columnIndex], right.row[columnIndex]) * factor);
  }, [indexedRows, sort, component.sortable]);

  function toggleSort(columnIndex: number) {
    if (!component.sortable) return;
    setSort((current) => {
      if (!current || current.columnIndex !== columnIndex) return { columnIndex, direction: 'ascending' };
      if (current.direction === 'ascending') return { columnIndex, direction: 'descending' };
      return null;
    });
  }

  return (
    <figure className="cei-table-figure">
      {component.title && <figcaption className="cei-table-title">{component.title}</figcaption>}
      <div className="cei-table-scroll">
        <table className="cei-table">
          {component.caption && <caption>{component.caption}</caption>}
          <thead>
            <tr>
              {component.columns.map((column, columnIndex) => {
                const ariaSort = component.sortable
                  ? (sort?.columnIndex === columnIndex ? sort.direction : 'none')
                  : undefined;
                return (
                  <th key={column.key} scope="col" data-align={effectiveAlign(column)} aria-sort={ariaSort}>
                    {component.sortable
                      ? <button type="button" className="cei-table-sort" onClick={() => toggleSort(columnIndex)}>{column.label}</button>
                      : column.label}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {sortedRows.map(({ row, rowIndex }) => (
              <tr key={rowIndex}>
                {row.map((cell, columnIndex) => {
                  const column = component.columns[columnIndex];
                  const flagged = flaggedCells.has(`${rowIndex}:${columnIndex}`);
                  return (
                    <td key={column.key} data-align={effectiveAlign(column)}>
                      {formatCell(cell)}
                      {flagged && <sup className="cei-table-flag" aria-label="not found in retrieved evidence">†</sup>}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {component.flagged_cells.length > 0 && <p className="cei-table-note">† Not found in retrieved evidence.</p>}
    </figure>
  );
}

// ADR: number columns right-align by default | schema does not force this, but a
// left-aligned number column reads poorly; only override when author left it default.
function effectiveAlign(column: TableComponent['columns'][number]): 'left' | 'right' | 'center' {
  if (column.kind === 'number' && column.align === 'left') return 'right';
  return column.align;
}

function formatCell(cell: TableCell): string {
  if (cell === null) return '';
  if (typeof cell === 'boolean') return cell ? 'Yes' : 'No';
  return String(cell);
}

function compareCells(left: TableCell, right: TableCell): number {
  if (left === right) return 0;
  if (left === null) return -1;
  if (right === null) return 1;
  if (typeof left === 'number' && typeof right === 'number') return left - right;
  return String(left).localeCompare(String(right));
}
