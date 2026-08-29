from __future__ import annotations

import os
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=PendingDeprecationWarning)

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "market_reaction.csv.xlsx"


class IngestionAgent:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load_data(self) -> pd.DataFrame:
        df = pd.read_excel(self.path)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
        return df.sort_values("date").reset_index(drop=True)


class AnalysisAgent:
    def analyze(self, df: pd.DataFrame) -> dict:
        insights = {
            "sp500_mean": float(df["sp500"].mean()),
            "oil_max": float(df["crude_oil_wti"].max()),
            "steel_avg": float(df["steel_futures"].mean()),
            "latest_sp500": float(df["sp500"].iloc[-1]),
            "correlation": df[["sp500", "crude_oil_wti", "steel_futures"]].corr(numeric_only=True).to_dict(),
        }
        return insights


try:
    from prophet import Prophet
except ImportError:  # pragma: no cover - runtime fallback
    Prophet = None


class ForecastAgent:
    def forecast_sp500(self, df: pd.DataFrame, periods: int = 30) -> pd.DataFrame:
        if Prophet is not None:
            data = df[["date", "sp500"]].copy()
            data.columns = ["ds", "y"]
            model = Prophet()
            model.fit(data)
            future = model.make_future_dataframe(periods=periods)
            forecast = model.predict(future)
            return forecast[["ds", "yhat", "yhat_lower", "yhat_upper"]]

        series = df["sp500"].dropna().astype(float)
        x = np.arange(len(series))
        slope, intercept = np.polyfit(x, series.to_numpy(), 1)
        future_x = np.arange(len(series), len(series) + periods)
        future_vals = intercept + slope * future_x
        forecast_dates = pd.date_range(df["date"].iloc[-1] + pd.Timedelta(days=1), periods=periods, freq="D")
        return pd.DataFrame({"ds": forecast_dates, "yhat": future_vals})


class AnomalyAgent:
    def detect(self, df: pd.DataFrame) -> pd.DataFrame:
        try:
            from sklearn.ensemble import IsolationForest
        except ImportError:
            numeric = df[["sp500", "crude_oil_wti", "steel_futures"]].apply(pd.to_numeric, errors="coerce")
            z_scores = (numeric - numeric.mean()) / numeric.std(ddof=0)
            anomaly_mask = z_scores.abs().max(axis=1) > 2.5
            return df.loc[anomaly_mask].copy()

        features = df[["sp500", "crude_oil_wti", "steel_futures"]].copy()
        model = IsolationForest(contamination=0.02, random_state=42)
        df_with_anomalies = df.copy()
        df_with_anomalies["anomaly"] = model.fit_predict(features)
        return df_with_anomalies[df_with_anomalies["anomaly"] == -1].copy()


class RAGAgent:
    def __init__(self):
        self.enabled = False

    def build_memory(self, insights: dict):
        try:
            from langchain_community.vectorstores import Chroma
            from langchain_core.documents import Document
            from langchain_huggingface import HuggingFaceEmbeddings
        except ImportError as exc:
            print(f"RAG skipped: optional dependency missing ({exc}).")
            return None

        docs = [Document(page_content=f"{key}: {value}") for key, value in insights.items()]
        embedding = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
        vectordb = Chroma.from_documents(docs, embedding, persist_directory=str(ROOT / "memory" / "chroma_db"))
        self.enabled = True
        return vectordb


class ReportAgent:
    def generate(self, insights: dict, anomalies: pd.DataFrame) -> str:
        report = (
            "MARKET ANALYSIS REPORT\n\n"
            f"Average S&P500: {insights['sp500_mean']}\n"
            f"Max Oil Price: {insights['oil_max']}\n"
            f"Average Steel Futures: {insights['steel_avg']}\n"
            f"Total Anomalies: {len(anomalies)}\n"
        )

        output_dir = ROOT / "outputs" / "reports"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "report.txt").write_text(report, encoding="utf-8")
        return report


class AgentState(dict):
    pass


try:
    from langgraph.graph import StateGraph

    workflow = StateGraph(AgentState)
    workflow.add_node("ingestion", lambda x: x)
    workflow.add_node("analysis", lambda x: x)
    workflow.add_node("forecast", lambda x: x)
    workflow.add_node("anomaly", lambda x: x)
    workflow.add_node("report", lambda x: x)
    workflow.set_entry_point("ingestion")
    workflow.add_edge("ingestion", "analysis")
    workflow.add_edge("analysis", "forecast")
    workflow.add_edge("forecast", "anomaly")
    workflow.add_edge("anomaly", "report")
    app = workflow.compile(checkpointer=None)
except ImportError:  # pragma: no cover - optional graph library
    app = None


def run_pipeline(path: str | Path = DATA_FILE) -> dict:
    loader = IngestionAgent(path)
    df = loader.load_data()
    print("Dataset Loaded Successfully")
    print(df.head())

    analysis_agent = AnalysisAgent()
    insights = analysis_agent.analyze(df)
    print("\nMarket Insights:")
    print(insights)

    forecast_agent = ForecastAgent()
    forecast = forecast_agent.forecast_sp500(df)
    print("\nForecast Generated")
    print(forecast.head())

    anomaly_agent = AnomalyAgent()
    anomalies = anomaly_agent.detect(df)
    print("\nAnomalies Detected:")
    print(anomalies.head())

    rag_agent = RAGAgent()
    rag_agent.build_memory(insights)
    print("\nRAG Memory Created Successfully")

    report_agent = ReportAgent()
    report = report_agent.generate(insights, anomalies)
    print("\nFinal Report:")
    print(report)

    return {"df": df, "insights": insights, "forecast": forecast, "anomalies": anomalies, "report": report}


if __name__ == "__main__":
    if not DATA_FILE.exists():
        raise FileNotFoundError(f"Dataset not found at {DATA_FILE}")
    run_pipeline(DATA_FILE)
