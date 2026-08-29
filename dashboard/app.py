from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "market_reaction.csv.xlsx"


def load_data(path: Path = DATA_FILE) -> pd.DataFrame:
    df = pd.read_excel(path)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date", na_position="last").reset_index(drop=True)
    return df


def compute_insights(df: pd.DataFrame) -> dict:
    return {
        "sp500_mean": float(df["sp500"].mean()),
        "oil_max": float(df["crude_oil_wti"].max()),
        "steel_avg": float(df["steel_futures"].mean()),
        "latest_sp500": float(df["sp500"].iloc[-1]),
        "latest_oil": float(df["crude_oil_wti"].iloc[-1]),
    }


def compute_forecast(df: pd.DataFrame, periods: int = 30) -> pd.DataFrame:
    series = df["sp500"].dropna().astype(float)
    x = np.arange(len(series))
    slope, intercept = np.polyfit(x, series.to_numpy(), 1)
    future_x = np.arange(len(series), len(series) + periods)
    future_vals = intercept + slope * future_x
    dates = pd.date_range(df["date"].iloc[-1] + pd.Timedelta(days=1), periods=periods, freq="D")
    return pd.DataFrame({"date": dates, "forecast": future_vals})


def detect_anomalies(df: pd.DataFrame) -> pd.DataFrame:
    numeric = df[["sp500", "crude_oil_wti", "steel_futures"]].apply(pd.to_numeric, errors="coerce")
    z_scores = (numeric - numeric.mean()) / numeric.std(ddof=0)
    anomaly_mask = z_scores.abs().max(axis=1) > 2.5
    return df.loc[anomaly_mask].copy()


def main() -> None:
    st.set_page_config(page_title="AI Market Intelligence", page_icon="📈", layout="wide")
    st.title("AI Market Intelligence Dashboard")

    try:
        df = load_data()
    except FileNotFoundError:
        st.error("Data file not found. Please place the Excel file in the project root as market_reaction.csv.xlsx.")
        return
    except Exception as exc:  # pragma: no cover - surfaced in UI feedback
        st.error(f"Failed to load dataset: {exc}")
        return

    insights = compute_insights(df)
    forecast = compute_forecast(df)
    anomalies = detect_anomalies(df)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Avg S&P 500", f"{insights['sp500_mean']:.2f}")
    col2.metric("Max Oil", f"{insights['oil_max']:.2f}")
    col3.metric("Avg Steel", f"{insights['steel_avg']:.2f}")
    col4.metric("Anomalies", len(anomalies))

    st.subheader("Market trend overview")
    market_chart = px.line(
        df,
        x="date",
        y=["sp500", "crude_oil_wti", "steel_futures"],
        title="Market data over time",
    )
    market_chart.update_layout(legend_title_text="Metric")
    st.plotly_chart(market_chart, use_container_width=True)

    st.subheader("Forecast")
    forecast_chart = px.line(
        forecast,
        x="date",
        y="forecast",
        title="30-day S&P 500 outlook",
    )
    st.plotly_chart(forecast_chart, use_container_width=True)

    with st.expander("Dataset preview"):
        st.dataframe(df.tail(10), use_container_width=True)

    with st.expander("Detected anomalies"):
        if anomalies.empty:
            st.info("No material anomalies detected in the current dataset.")
        else:
            st.dataframe(anomalies, use_container_width=True)

    st.caption("Data source: market_reaction.csv.xlsx")


if __name__ == "__main__":
    main()
