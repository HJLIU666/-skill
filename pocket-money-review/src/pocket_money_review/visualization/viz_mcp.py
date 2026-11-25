"""Dependency-free offline visualization for confirmed review datasets.

``VizMcpClient`` exposes the async, tool-shaped method used by the review
orchestrator. It accepts either a confirmed category summary or a confirmed
time series and generates one self-contained HTML file with inline CSS and
JavaScript. No CDN, network request, raw expense description, or historical
detail is placed in the file.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Mapping

VisualizationOperationStatus = Literal[
    "generated",
    "unavailable",
    "generation-failed",
    "file-inaccessible",
    "pending",
]
VisualizationDataKind = Literal["category-summary", "time-series"]
ChartType = Literal["pie", "bar", "line"]


class VisualizationPendingError(ValueError):
    """The caller has not confirmed data or the visualization gate."""


def _decimal(value: Decimal | int | float | str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc
    if not result.is_finite():
        raise ValueError("visualization numbers must be finite")
    return result


@dataclass(frozen=True)
class VisualizationCategory:
    name: str
    amount: Decimal
    percentage: Decimal
    confirmation_state: Literal["confirmed"] = "confirmed"

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "amount": str(self.amount),
            "percentage": str(self.percentage),
            "confirmation_state": self.confirmation_state,
        }


@dataclass(frozen=True)
class VisualizationPoint:
    label: str
    value: Decimal
    confirmation_state: Literal["confirmed"] = "confirmed"

    def to_dict(self) -> dict[str, str]:
        return {
            "label": self.label,
            "value": str(self.value),
            "confirmation_state": self.confirmation_state,
        }


@dataclass(frozen=True)
class VisualizationDataset:
    """Confirmed aggregate data allowed into a chart."""

    review_period: dict[str, str]
    currency: str
    total_spending: Decimal | None
    categories: tuple[VisualizationCategory, ...]
    series: tuple[VisualizationPoint, ...]
    source_snapshot_id: str
    data_kind: VisualizationDataKind

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "VisualizationDataset":
        if value.get("confirmation_state") != "confirmed":
            raise VisualizationPendingError("the visualization dataset is not confirmed")
        if value.get("visualization_confirmation") != "confirmed":
            raise VisualizationPendingError("visualization confirmation is required")

        period = value.get("review_period", value.get("reviewPeriod"))
        if not isinstance(period, Mapping) or not period.get("start") or not period.get("end"):
            raise ValueError("review_period with start and end is required")
        currency = str(value.get("currency", "")).strip()
        if not currency:
            raise ValueError("currency is required")
        snapshot_id = str(
            value.get("source_snapshot_id", value.get("sourceSnapshotId", ""))
        ).strip()
        if not snapshot_id:
            raise ValueError("source_snapshot_id is required")

        raw_kind = str(value.get("type", value.get("chart_kind", ""))).strip().lower()
        has_series = "series" in value or "points" in value
        has_categories = "categories" in value
        if raw_kind in {"time-series", "trend", "line"} or has_series:
            data_kind: VisualizationDataKind = "time-series"
        elif raw_kind in {"category-summary", "category", "categories", ""} or has_categories:
            data_kind = "category-summary"
        else:
            raise ValueError(
                "cannot detect data structure; provide categories or series"
            )

        raw_total = value.get("total_spending", value.get("totalSpending"))
        total = _decimal(raw_total) if raw_total is not None else None
        if total is not None and total < 0:
            raise ValueError("total_spending cannot be negative")

        categories: tuple[VisualizationCategory, ...] = ()
        series: tuple[VisualizationPoint, ...] = ()
        if data_kind == "category-summary":
            categories = cls._parse_categories(value.get("categories", []), total)
            category_total = sum(
                (item.amount for item in categories), Decimal("0")
            )
            if total is None:
                total = category_total
            if category_total != total:
                raise ValueError("category amounts must equal total_spending")
        else:
            series = cls._parse_series(value.get("series", value.get("points")))
            if not series:
                raise ValueError("time-series data requires at least one point")

        return cls(
            review_period={
                "start": str(period["start"]),
                "end": str(period["end"]),
            },
            currency=currency,
            total_spending=total,
            categories=categories,
            series=series,
            source_snapshot_id=snapshot_id,
            data_kind=data_kind,
        )

    @staticmethod
    def _parse_categories(
        raw_categories: Any,
        total: Decimal | None,
    ) -> tuple[VisualizationCategory, ...]:
        if isinstance(raw_categories, Mapping):
            raw_categories = [
                {"name": name, "amount": amount}
                for name, amount in raw_categories.items()
            ]
        if not isinstance(raw_categories, list):
            raise ValueError("categories must be an array or object")

        categories: list[VisualizationCategory] = []
        for raw in raw_categories:
            if not isinstance(raw, Mapping):
                raise ValueError("each category must be an object")
            if raw.get("confirmation_state", "confirmed") != "confirmed":
                raise VisualizationPendingError(
                    "unconfirmed category cannot enter a chart"
                )
            name = str(raw.get("name", raw.get("category", ""))).strip()
            if not name:
                raise ValueError("category name must not be empty")
            amount = _decimal(raw.get("amount"))
            if amount < 0:
                raise ValueError("category amount cannot be negative")
            percentage_value = raw.get("percentage")
            if percentage_value is None:
                percentage = (
                    (amount / total * Decimal("100"))
                    if total and total != 0
                    else Decimal("0")
                )
            else:
                percentage = _decimal(percentage_value)
            if percentage < 0:
                raise ValueError("category percentage cannot be negative")
            categories.append(VisualizationCategory(name, amount, percentage))
        return tuple(categories)

    @staticmethod
    def _parse_series(raw_series: Any) -> tuple[VisualizationPoint, ...]:
        if isinstance(raw_series, Mapping):
            raw_series = [
                {"label": label, "value": value}
                for label, value in raw_series.items()
            ]
        if not isinstance(raw_series, list):
            raise ValueError("series must be an array or object")

        points: list[VisualizationPoint] = []
        for raw in raw_series:
            if not isinstance(raw, Mapping):
                raise ValueError("each time-series point must be an object")
            if raw.get("confirmation_state", "confirmed") != "confirmed":
                raise VisualizationPendingError(
                    "unconfirmed time-series point cannot enter a chart"
                )
            label = str(
                raw.get("label", raw.get("date", raw.get("period", raw.get("x", ""))))
            ).strip()
            if not label:
                raise ValueError("time-series point requires a label or date")
            raw_value = raw.get("value", raw.get("amount", raw.get("y")))
            if raw_value is None:
                raise ValueError("time-series point requires a value")
            point_value = _decimal(raw_value)
            if point_value < 0:
                raise ValueError("time-series spending values cannot be negative")
            points.append(VisualizationPoint(label, point_value))
        return tuple(points)

    def public_payload(self) -> dict[str, Any]:
        """Return data safe to embed; raw record IDs are intentionally omitted."""

        return {
            "type": self.data_kind,
            "review_period": self.review_period,
            "currency": self.currency,
            "total_spending": (
                str(self.total_spending) if self.total_spending is not None else None
            ),
            "categories": [item.to_dict() for item in self.categories],
            "series": [item.to_dict() for item in self.series],
            "source_snapshot_id": self.source_snapshot_id,
            "confirmation_state": "confirmed",
        }


@dataclass(frozen=True)
class VisualizationResult:
    operation_status: VisualizationOperationStatus
    local_html_reference: str | None = None
    self_contained: bool = False
    included_snapshot_id: str | None = None
    chart_type: ChartType | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_status": self.operation_status,
            "local_html_reference": self.local_html_reference,
            "self_contained": self.self_contained,
            "included_snapshot_id": self.included_snapshot_id,
            "chart_type": self.chart_type,
            "message": self.message,
        }


class VizMcpClient:
    """Create local, self-contained HTML from confirmed data."""

    def __init__(self, output_dir: str | Path = "data/charts") -> None:
        self.output_dir = Path(output_dir).expanduser()

    @staticmethod
    def choose_chart_type(dataset: VisualizationDataset) -> ChartType:
        """Choose a chart from the normalized data shape, not a caller hint."""

        if dataset.data_kind == "time-series":
            return "line"
        return "pie" if len(dataset.categories) <= 6 else "bar"

    async def visualize(
        self,
        dataset: VisualizationDataset | Mapping[str, Any],
        output_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Generate the chart and return the status consumed by the report."""

        try:
            parsed = (
                dataset
                if isinstance(dataset, VisualizationDataset)
                else VisualizationDataset.from_mapping(dataset)
            )
            chart_type = self.choose_chart_type(parsed)
            prefix = "trend" if parsed.data_kind == "time-series" else "category"
            target = Path(output_path) if output_path else (
                self.output_dir / f"{prefix}-{parsed.source_snapshot_id}.html"
            )
            html_text = self._render_html(parsed)
            written_path = await asyncio.to_thread(self._write_atomic, target, html_text)
            return VisualizationResult(
                operation_status="generated",
                local_html_reference=str(written_path),
                self_contained=True,
                included_snapshot_id=parsed.source_snapshot_id,
                chart_type=chart_type,
                message=f"chart generated as {chart_type}",
            ).to_dict()
        except VisualizationPendingError as exc:
            return VisualizationResult(
                operation_status="pending", message=str(exc)
            ).to_dict()
        except PermissionError as exc:
            return VisualizationResult(
                operation_status="file-inaccessible", message=str(exc)
            ).to_dict()
        except (OSError, ValueError, TypeError) as exc:
            return VisualizationResult(
                operation_status="generation-failed", message=str(exc)
            ).to_dict()

    @staticmethod
    def _write_atomic(target: Path, content: str) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = temporary.name
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, target)
            temporary_path = None
            return target
        finally:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

    def _render_html(self, dataset: VisualizationDataset) -> str:
        payload = dataset.public_payload()
        payload["selected_chart_type"] = self.choose_chart_type(dataset)
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        serialized = (
            serialized.replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
        )
        title = html.escape(
            f"Pocket-money review · {dataset.review_period['start']} to "
            f"{dataset.review_period['end']}"
        )
        template = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root { color-scheme: light; font-family: system-ui, sans-serif; }
body { margin: 0; padding: 2rem; color: #172033; background: #f5f7fb; }
main { max-width: 960px; margin: auto; background: white; padding: 1.5rem; border-radius: 16px; box-shadow: 0 8px 30px #17203318; }
.meta { color: #586174; margin: .25rem 0 1.2rem; }
.controls { display: flex; gap: .5rem; margin-bottom: 1rem; }
button { border: 1px solid #c7cfdf; background: white; color: #172033; border-radius: 999px; padding: .45rem .9rem; cursor: pointer; }
button.active, button:hover { background: #315efb; color: white; border-color: #315efb; }
#chart { min-height: 280px; display: grid; place-items: center; }
.pie { width: 250px; height: 250px; border-radius: 50%; box-shadow: inset 0 0 0 1px #0001; }
.bars { width: 100%; display: grid; gap: .75rem; }
.bar-row { display: grid; grid-template-columns: minmax(7rem, 12rem) 1fr 6rem; align-items: center; gap: .75rem; }
.bar-track { height: 1.2rem; background: #edf0f7; border-radius: 999px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: inherit; transition: width .2s ease; }
.line-chart { width: 100%; overflow-x: auto; }
.line-chart svg { width: 100%; min-width: 620px; height: 360px; display: block; }
.line-grid { stroke: #e5e9f2; stroke-width: 1; }
.line-axis { stroke: #9aa5b8; stroke-width: 1.2; }
.line-path { fill: none; stroke: #315efb; stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; }
.line-point { fill: white; stroke: #315efb; stroke-width: 3; }
.line-label { fill: #586174; font-size: 12px; }
.legend { display: grid; grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr)); gap: .45rem; margin-top: 1.25rem; }
.legend-item { display: flex; align-items: center; gap: .45rem; color: #3e4758; }
.swatch { width: .75rem; height: .75rem; border-radius: 3px; }
footer { color: #697386; font-size: .85rem; margin-top: 1.5rem; }
@media (max-width: 640px) { body { padding: .75rem; } .bar-row { grid-template-columns: 6rem 1fr 4rem; gap: .4rem; font-size: .9rem; } }
</style>
</head>
<body>
<main>
<h1>__TITLE__</h1>
<p class="meta" id="meta"></p>
<div class="controls" id="controls" aria-label="Chart type"></div>
<section id="chart" aria-live="polite"></section>
<section class="legend" id="legend"></section>
<footer>Chart type: <code id="chart-type"></code> · Snapshot: <code id="snapshot"></code></footer>
<script id="chart-data" type="application/json">__DATA__</script>
<script>
(() => {
  const data = JSON.parse(document.getElementById('chart-data').textContent);
  const chart = document.getElementById('chart');
  const legend = document.getElementById('legend');
  const controls = document.getElementById('controls');
  const colors = ['#315efb', '#f08a4b', '#36a269', '#9b5de5', '#e0b43b', '#e05276', '#4aa3df', '#6d798f'];
  const total = data.total_spending === null ? null : Number(data.total_spending);
  const categories = data.categories.map((item, index) => ({
    ...item,
    number: Number(item.amount),
    color: colors[index % colors.length]
  }));
  const series = data.series.map(item => ({ ...item, number: Number(item.value) }));

  const metaText = data.type === 'time-series'
    ? `${data.review_period.start} to ${data.review_period.end} · ${data.currency} · ${series.length} points`
    : `${data.review_period.start} to ${data.review_period.end} · ${data.currency} · total ${data.total_spending}`;
  document.getElementById('meta').textContent = metaText;
  document.getElementById('chart-type').textContent = data.selected_chart_type;
  document.getElementById('snapshot').textContent = data.source_snapshot_id;

  function renderLegend() {
    legend.replaceChildren();
    if (data.type === 'time-series') {
      series.forEach(item => {
        const row = document.createElement('div');
        row.className = 'legend-item';
        const swatch = document.createElement('span');
        swatch.className = 'swatch';
        swatch.style.background = '#315efb';
        const label = document.createElement('span');
        label.textContent = `${item.label}: ${item.value} ${data.currency}`;
        row.append(swatch, label);
        legend.append(row);
      });
      return;
    }
    categories.forEach(item => {
      const row = document.createElement('div');
      row.className = 'legend-item';
      const swatch = document.createElement('span');
      swatch.className = 'swatch';
      swatch.style.background = item.color;
      const label = document.createElement('span');
      label.textContent = `${item.name}: ${item.amount} (${item.percentage}%)`;
      row.append(swatch, label);
      legend.append(row);
    });
  }

  function renderPie() {
    const stops = [];
    let cursor = 0;
    categories.forEach(item => {
      const share = total ? item.number / total * 100 : 0;
      stops.push(`${item.color} ${cursor}% ${cursor + share}%`);
      cursor += share;
    });
    chart.replaceChildren();
    const pie = document.createElement('div');
    pie.className = 'pie';
    pie.setAttribute('role', 'img');
    pie.setAttribute('aria-label', 'Category spending pie chart');
    pie.style.background = `conic-gradient(${stops.join(', ')})`;
    chart.append(pie);
  }

  function renderBars() {
    chart.replaceChildren();
    const bars = document.createElement('div');
    bars.className = 'bars';
    const max = Math.max(...categories.map(item => item.number), 0);
    categories.forEach(item => {
      const row = document.createElement('div');
      row.className = 'bar-row';
      const name = document.createElement('span');
      name.textContent = item.name;
      const track = document.createElement('div');
      track.className = 'bar-track';
      const fill = document.createElement('div');
      fill.className = 'bar-fill';
      fill.style.background = item.color;
      fill.style.width = `${max ? item.number / max * 100 : 0}%`;
      fill.title = `${item.name}: ${item.amount}`;
      track.append(fill);
      const amount = document.createElement('strong');
      amount.textContent = `${item.amount} ${data.currency}`;
      row.append(name, track, amount);
      bars.append(row);
    });
    chart.append(bars);
  }

  function svgText(svg, value, x, y, anchor = 'middle') {
    const node = document.createElementNS('http' + '://www.w3.org/2000/svg', 'text');
    node.setAttribute('x', x);
    node.setAttribute('y', y);
    node.setAttribute('text-anchor', anchor);
    node.setAttribute('class', 'line-label');
    node.textContent = value;
    svg.append(node);
  }

  function renderLine() {
    chart.replaceChildren();
    const wrapper = document.createElement('div');
    wrapper.className = 'line-chart';
    const svgNamespace = 'http' + '://www.w3.org/2000/svg';
    const svg = document.createElementNS(svgNamespace, 'svg');
    svg.setAttribute('viewBox', '0 0 800 360');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', 'Spending trend line chart');
    const width = 800;
    const height = 360;
    const left = 58;
    const right = 24;
    const top = 24;
    const bottom = 62;
    const values = series.map(item => item.number);
    const min = Math.min(...values, 0);
    const max = Math.max(...values, 1);
    const range = max - min || 1;
    const x = index => series.length === 1
      ? (width + left - right) / 2
      : left + index * (width - left - right) / (series.length - 1);
    const y = value => height - bottom - (value - min) / range * (height - top - bottom);

    for (let index = 0; index <= 4; index += 1) {
      const value = min + range * index / 4;
      const yPosition = y(value);
      const grid = document.createElementNS(svgNamespace, 'line');
      grid.setAttribute('x1', left);
      grid.setAttribute('x2', width - right);
      grid.setAttribute('y1', yPosition);
      grid.setAttribute('y2', yPosition);
      grid.setAttribute('class', 'line-grid');
      svg.append(grid);
      svgText(svg, value.toFixed(2), left - 8, yPosition + 4, 'end');
    }

    const axis = document.createElementNS(svgNamespace, 'line');
    axis.setAttribute('x1', left);
    axis.setAttribute('x2', width - right);
    axis.setAttribute('y1', height - bottom);
    axis.setAttribute('y2', height - bottom);
    axis.setAttribute('class', 'line-axis');
    svg.append(axis);

    const points = series.map((item, index) => `${x(index)},${y(item.number)}`).join(' ');
    const path = document.createElementNS(svgNamespace, 'polyline');
    path.setAttribute('points', points);
    path.setAttribute('class', 'line-path');
    svg.append(path);

    series.forEach((item, index) => {
      const point = document.createElementNS(svgNamespace, 'circle');
      point.setAttribute('cx', x(index));
      point.setAttribute('cy', y(item.number));
      point.setAttribute('r', '5');
      point.setAttribute('class', 'line-point');
      const title = document.createElementNS(svgNamespace, 'title');
      title.textContent = `${item.label}: ${item.value} ${data.currency}`;
      point.append(title);
      svg.append(point);
      svgText(svg, item.label, x(index), height - bottom + 28);
    });
    wrapper.append(svg);
    chart.append(wrapper);
  }

  function select(mode) {
    document.querySelectorAll('[data-mode]').forEach(button => {
      button.classList.toggle('active', button.dataset.mode === mode);
    });
    if (mode === 'line') renderLine();
    else if (mode === 'pie') renderPie();
    else renderBars();
    renderLegend();
  }

  if (data.selected_chart_type === 'line') {
    controls.hidden = true;
    select('line');
  } else {
    ['pie', 'bar'].forEach(mode => {
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.mode = mode;
      button.textContent = mode === 'pie' ? 'Pie' : 'Bar';
      button.addEventListener('click', () => select(mode));
      controls.append(button);
    });
    select(data.selected_chart_type);
  }
})();
</script>
</main>
</body>
</html>
'''
        return template.replace("__TITLE__", title).replace("__DATA__", serialized)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate an offline adaptive chart")
    parser.add_argument("--input", type=Path, required=True, help="dataset JSON file")
    parser.add_argument("--output", type=Path, default=None, help="HTML output file")
    args = parser.parse_args(argv)
    dataset = json.loads(args.input.read_text(encoding="utf-8-sig"))
    result = asyncio.run(VizMcpClient().visualize(dataset, args.output))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["operation_status"] == "generated" else 1


if __name__ == "__main__":
    raise SystemExit(main())
