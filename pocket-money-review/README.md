# pocket-money-review

The Skill entry document and Python adapters for pocket-money review.

## Project structure

```text
pocket-money-review/
├─ src/
│  └─ pocket_money_review/
│     ├─ SKILL.md
│     ├─ history/
│     │  └─ mcp_bill_track.py
│     └─ visualization/
│        └─ viz_mcp.py
├─ tests/
└─ README.md
```

`src/pocket_money_review/SKILL.md` is the Skill's main instruction document. It defines triggers, intake questions, confirmation gates, report format, privacy rules and adapter behavior. The project does not depend on a separate specification directory for its Skill instructions.

## Skill behavior

The Skill starts when the user asks about weekly or monthly pocket-money spending, overspending, impulse-spending candidates or saving toward a goal. It confirms the review period and income before querying local history, asks the user to confirm categories, and produces a Markdown review with optional offline charts.

The main behavior and safety rules are maintained in `src/pocket_money_review/SKILL.md`.

## Run the adapters

From the `pocket-money-review` directory, set the source path and inspect the command help:

```powershell
$env:PYTHONPATH = "src"
python -m pocket_money_review.history.mcp_bill_track --help
python -m pocket_money_review.visualization.viz_mcp --help
```

Both adapters use the Python standard library. `mcp_bill_track` stores confirmed transactions and review summaries in local JSON. `viz_mcp` generates self-contained offline HTML charts from confirmed data:

- `categories` data with up to six categories selects a pie chart;
- `categories` data with more than six categories selects a bar chart;
- `series` or `points` data selects a line chart.

The generated result reports the selected chart type and keeps the chart data inside the HTML file.
