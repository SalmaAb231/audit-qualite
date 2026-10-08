"""Plotly charts with one consistent, dashboard-wide visual language."""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


BLUE = "#2563eb"
PURPLE = "#7c3aed"
GREEN = "#16a34a"
AMBER = "#f59e0b"
RED = "#ef4444"
SLATE = "#64748b"
GRID = "#e2e8f0"
TEXT = "#17223b"
OUTCOME_COLORS = {
    "ACCEPTED": GREEN,
    "WARNING": AMBER,
    "REJECTED": RED,
    "INCOMPLETE": "#8aa0bf",
}
SEVERITY_COLORS = {
    "CRITICAL": RED,
    "HIGH": "#ea580c",
    "MEDIUM": AMBER,
    "LOW": SLATE,
    "UNKNOWN": "#94a3b8",
}
AGENT_STATUS_COLORS = {
    "SUCCESS": GREEN,
    "FAILED": RED,
    "NOT_RUN": SLATE,
    "UNKNOWN": "#94a3b8",
}
AGENT_COLORS = {
    "Structure": GREEN,
    "Statistics": BLUE,
    "Coherence": PURPLE,
    "Realism": AMBER,
}


def get_severity_color(severity: object) -> str:
    return SEVERITY_COLORS.get(str(severity or "UNKNOWN").upper(), SLATE)


def get_agent_success_color(value: object) -> str:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return "#94a3b8"
    if float(numeric) >= 90:
        return GREEN
    if float(numeric) >= 70:
        return AMBER
    return RED


def _layout(fig: go.Figure, *, height: int, show_legend: bool = False) -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=12, r=18, t=14, b=18),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter, Segoe UI, sans-serif", color=TEXT, size=12),
        showlegend=show_legend,
        hoverlabel=dict(bgcolor="white", font_size=12),
    )
    fig.update_xaxes(gridcolor=GRID, zeroline=False, automargin=True)
    fig.update_yaxes(gridcolor=GRID, zeroline=False, automargin=True)
    return fig


def show(fig: go.Figure) -> None:
    st.plotly_chart(
        fig,
        use_container_width=True,
        config={"displayModeBar": False, "responsive": True},
    )


def donut(counts: Mapping[str, int] | pd.Series, *, center_label: str = "audits") -> go.Figure:
    order = ["ACCEPTED", "WARNING", "REJECTED", "INCOMPLETE"]
    values = [int(counts.get(key, 0) or 0) for key in order]
    fig = go.Figure(
        go.Pie(
            labels=[key.title() for key in order],
            values=values,
            hole=0.62,
            sort=False,
            textinfo="percent",
            textposition="inside",
            marker=dict(colors=[OUTCOME_COLORS[key] for key in order], line=dict(color="white", width=2)),
            hovertemplate="%{label}: %{value} (%{percent})<extra></extra>",
        )
    )
    total = sum(values)
    fig.add_annotation(
        text=f"<b>{total}</b><br><span style='font-size:11px'>{center_label}</span>",
        x=0.5,
        y=0.5,
        showarrow=False,
        font=dict(size=18, color=TEXT),
    )
    fig.update_layout(legend=dict(orientation="v", y=0.5, x=1.02, yanchor="middle"))
    return _layout(fig, height=240, show_legend=True)


def horizontal_bars(
    series: pd.Series,
    *,
    value_title: str,
    color: str = BLUE,
    color_map: Mapping[str, str] | None = None,
    height: int | None = None,
    preserve_order: bool = False,
) -> go.Figure:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if not preserve_order:
        values = values.sort_values(ascending=True)
    labels = [str(value) for value in values.index]
    colors = [color_map.get(label.upper(), color) for label in labels] if color_map else color
    fig = go.Figure(
        go.Bar(
            x=values.tolist(),
            y=labels,
            orientation="h",
            marker=dict(color=colors, line=dict(width=0)),
            text=[f"{value:,.0f}" for value in values],
            textposition="outside",
            cliponaxis=False,
            customdata=labels,
            hovertemplate="%{customdata}<br>" + value_title + ": %{x:,.0f}<extra></extra>",
        )
    )
    fig.update_xaxes(title=value_title, rangemode="tozero", tickformat=",d")
    fig.update_yaxes(title=None)
    if preserve_order:
        fig.update_yaxes(autorange="reversed")
    return _layout(fig, height=height or max(180, 34 * len(values) + 62))


def line_series(
    series: pd.Series,
    *,
    value_title: str,
    color: str = BLUE,
    decimals: int = 0,
) -> go.Figure:
    data = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(series.index, errors="coerce", utc=True),
            "value": pd.to_numeric(series.values, errors="coerce"),
        }
    ).dropna().sort_values("timestamp")
    hover_format = ",.2f" if decimals else ",.0f"
    fig = go.Figure(
        go.Scatter(
            x=data["timestamp"],
            y=data["value"],
            mode="lines+markers",
            line=dict(color=color, width=2.5),
            marker=dict(size=7, color=color, line=dict(color="white", width=1.5)),
            hovertemplate="%{x|%d %b %Y, %H:%M UTC}<br>"
            + value_title
            + f": %{{y:{hover_format}}}<extra></extra>",
        )
    )
    fig.update_xaxes(title="Audit time (UTC)", tickformat="%d %b\n%H:%M")
    fig.update_yaxes(title=value_title, rangemode="tozero")
    return _layout(fig, height=235)


def multi_line(
    frame: pd.DataFrame,
    *,
    x: str,
    y: str,
    group: str,
    value_title: str,
    colors: Mapping[str, str] | None = None,
) -> go.Figure:
    fig = go.Figure()
    for name, subset in frame.sort_values(x).groupby(group):
        label = str(name)
        fig.add_trace(
            go.Scatter(
                x=subset[x],
                y=subset[y],
                name=label,
                mode="lines+markers",
                line=dict(width=2.2, color=(colors or {}).get(label)),
                marker=dict(size=6),
                hovertemplate="%{x|%d %b %Y, %H:%M UTC}<br>"
                + label
                + ": %{y:,.0f}<extra></extra>",
            )
        )
    fig.update_xaxes(title="Audit time (UTC)", tickformat="%d %b\n%H:%M")
    fig.update_yaxes(title=value_title, rangemode="tozero")
    fig.update_layout(legend=dict(orientation="h", y=1.04, x=0))
    return _layout(fig, height=260, show_legend=True)


def anomalies_per_audit(frame: pd.DataFrame) -> go.Figure:
    """Independent audit points; no line implies a cross-file time trend."""
    data = frame.copy()
    timestamps = data.get("timestamp", pd.Series(pd.NaT, index=data.index))
    anomalies = data.get("anomalies_total", pd.Series(pd.NA, index=data.index))
    data["_timestamp"] = pd.to_datetime(timestamps, errors="coerce", utc=True)
    data["_anomalies"] = pd.to_numeric(anomalies, errors="coerce")
    data = data.dropna(subset=["_timestamp", "_anomalies"]).sort_values("_timestamp")
    data["_outcome"] = data.get(
        "status", data.get("verdict", pd.Series("INCOMPLETE", index=data.index))
    ).fillna("INCOMPLETE").astype(str).str.upper()
    # Only truly overlapping markers receive a tiny display offset. Hover keeps the persisted time.
    overlap_rank = data.groupby(["_timestamp", "_anomalies"]).cumcount()
    data["_plot_timestamp"] = data["_timestamp"] + pd.to_timedelta(overlap_rank, unit="s")
    data["_audit_label"] = data["_timestamp"].dt.strftime("%d %b %Y, %H:%M:%S UTC")
    data["_filename"] = data.get("filename", pd.Series("N/A", index=data.index)).fillna("N/A")
    data["_load_lock"] = data.get("load_lock", pd.Series("N/A", index=data.index)).fillna("N/A")
    data["_pipeline"] = data.get("pipeline_status", pd.Series("N/A", index=data.index)).fillna("N/A")
    data["_verdict"] = data.get("verdict", pd.Series("N/A", index=data.index)).fillna("N/A")
    scores = pd.to_numeric(data.get("score", pd.Series(pd.NA, index=data.index)), errors="coerce")
    data["_score"] = scores.map(lambda value: "N/A" if pd.isna(value) else f"{value:.2f}")

    fig = go.Figure()
    outcome_order = ["ACCEPTED", "WARNING", "REJECTED", "INCOMPLETE"]
    remaining = [value for value in data["_outcome"].unique() if value not in outcome_order]
    for outcome in [*outcome_order, *remaining]:
        subset = data[data["_outcome"] == outcome]
        if subset.empty:
            continue
        customdata = list(
            zip(
                subset["_filename"], subset["_load_lock"], subset["_audit_label"],
                subset["_pipeline"], subset["_verdict"], subset["_score"],
            )
        )
        fig.add_trace(
            go.Scatter(
                x=subset["_plot_timestamp"],
                y=subset["_anomalies"],
                name=outcome.title(),
                mode="markers",
                marker=dict(
                    size=10,
                    color=OUTCOME_COLORS.get(outcome, SLATE),
                    opacity=0.88,
                    line=dict(color="white", width=1.5),
                ),
                customdata=customdata,
                hovertemplate=(
                    "File: %{customdata[0]}<br>"
                    "LOAD_LOCK: %{customdata[1]}<br>"
                    "Audit time: %{customdata[2]}<br>"
                    "Anomalies: %{y:,.0f}<br>"
                    "Pipeline status: %{customdata[3]}<br>"
                    "Judge verdict: %{customdata[4]}<br>"
                    "Quality score: %{customdata[5]}<extra></extra>"
                ),
            )
        )
    fig.update_xaxes(title="Audit time (UTC)", tickformat="%d %b\n%H:%M")
    fig.update_yaxes(title="Anomalies detected", rangemode="tozero")
    fig.update_layout(legend=dict(orientation="h", y=1.06, x=0))
    return _layout(fig, height=260, show_legend=True)


def agent_detections_by_audit(frame: pd.DataFrame) -> go.Figure:
    """One marker for one persisted agent count in one audit record."""
    fig = go.Figure()
    extra_agents = list(
        dict.fromkeys(str(value) for value in frame.get("agent", []) if value not in AGENT_COLORS)
    )
    for agent in [*AGENT_COLORS, *extra_agents]:
        subset = frame[frame["agent"] == agent].sort_values("audit_time")
        if subset.empty:
            continue
        audit_labels = pd.to_datetime(subset["audit_time"], utc=True).dt.strftime(
            "%d %b %Y, %H:%M:%S UTC"
        )
        customdata = list(
            zip(
                subset["filename"].fillna("N/A"),
                subset["load_lock"].fillna("N/A"),
                audit_labels,
                subset["agent_status"].fillna("UNKNOWN"),
                subset["verdict"].fillna("N/A"),
                subset["pipeline_status"].fillna("N/A"),
            )
        )
        fig.add_trace(
            go.Scatter(
                x=subset["audit_time"],
                y=subset["detections"],
                name=str(agent),
                mode="markers",
                marker=dict(
                    size=9,
                    color=AGENT_COLORS.get(str(agent), SLATE),
                    opacity=0.86,
                    line=dict(color="white", width=1.25),
                ),
                customdata=customdata,
                hovertemplate=(
                    "Agent: " + str(agent) + "<br>"
                    "File: %{customdata[0]}<br>"
                    "LOAD_LOCK: %{customdata[1]}<br>"
                    "Audit time: %{customdata[2]}<br>"
                    "Detections: %{y:,.0f}<br>"
                    "Agent status: %{customdata[3]}<br>"
                    "Judge verdict: %{customdata[4]}<br>"
                    "Pipeline status: %{customdata[5]}<extra></extra>"
                ),
            )
        )
    fig.update_xaxes(title="Audit time (UTC)", tickformat="%d %b\n%H:%M")
    fig.update_yaxes(title="Detections", rangemode="tozero")
    fig.update_layout(legend=dict(orientation="h", y=1.06, x=0))
    return _layout(fig, height=260, show_legend=True)


def stacked_horizontal(
    frame: pd.DataFrame,
    *,
    category: str,
    value: str,
    stack: str,
    value_title: str,
    colors: Mapping[str, str],
) -> go.Figure:
    fig = go.Figure()
    for name in colors:
        subset = frame[frame[stack].astype(str).str.upper() == name.upper()]
        if subset.empty:
            continue
        fig.add_trace(
            go.Bar(
                x=subset[value],
                y=subset[category],
                name=name.title(),
                orientation="h",
                marker_color=colors[name],
                hovertemplate="%{y}<br>" + name.title() + ": %{x:,.0f}<extra></extra>",
            )
        )
    fig.update_layout(barmode="stack", legend=dict(orientation="h", y=1.04, x=0))
    fig.update_xaxes(title=value_title, rangemode="tozero")
    fig.update_yaxes(title=None)
    return _layout(fig, height=max(210, 34 * frame[category].nunique() + 80), show_legend=True)
