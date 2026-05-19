import pandas as pd
import warnings
import os
warnings.filterwarnings("ignore", category=PendingDeprecationWarning)

class IngestionAgent:

    def __init__(self, path):
        self.path = path

    def load_data(self):
        df = pd.read_excel(self.path)

        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date')

        return df
    
import pandas as pd

class AnalysisAgent:

    def analyze(self, df):

        insights = {
            "sp500_mean": df['sp500'].mean(),
            "oil_max": df['crude_oil_wti'].max(),
            "steel_avg": df['steel_futures'].mean(),
            "correlation": df.corr(numeric_only=True).to_dict()
        }

        return insights
    

from prophet import Prophet
import pandas as pd

class ForecastAgent:

    def forecast_sp500(self, df):

        data = df[['date', 'sp500']]

        data.columns = ['ds', 'y']

        model = Prophet()
        model.fit(data)

        future = model.make_future_dataframe(periods=30)

        forecast = model.predict(future)

        return forecast[['ds', 'yhat', 'yhat_lower', 'yhat_upper']]
    

import sklearn
import sklearn.ensemble
from sklearn.ensemble import IsolationForest

class AnomalyAgent:

    def detect(self, df):

        features = df[[
            'sp500',
            'crude_oil_wti',
            'steel_futures'
        ]]

        model = IsolationForest(contamination=0.02)

        df['anomaly'] = model.fit_predict(features)

        anomalies = df[df['anomaly'] == -1]

        return anomalies
    
from langchain_community.vectorstores import Chroma
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_core.documents import Document

class RAGAgent:

    def build_memory(self, insights):

        docs = []

        for key, value in insights.items():
            docs.append(Document(page_content=f"{key}: {value}"))

        # Ensure HF_TOKEN is set in your environment for authenticated Hugging Face Hub requests.
        embedding = HuggingFaceEmbeddings(
            model_name="sentence-transformers/all-MiniLM-L6-v2"
        )

        vectordb = Chroma.from_documents(
            docs,
            embedding,
            persist_directory="memory/chroma_db"
        )

        # Chroma 0.4+ persists automatically; manual persist() is no longer needed.
        return vectordb
    
class ReportAgent:

    def generate(self, insights, anomalies):

        report = f'''

        MARKET ANALYSIS REPORT

        Average S&P500: {insights['sp500_mean']}

        Max Oil Price: {insights['oil_max']}

        Average Steel Futures: {insights['steel_avg']}

        Total Anomalies: {len(anomalies)}

        '''

        os.makedirs("outputs/reports", exist_ok=True)
        with open("outputs/reports/report.txt", "w") as f:
            f.write(report)

        return report
    
from langgraph.graph import StateGraph

class AgentState(dict):
    pass

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


import streamlit as st
import pandas as pd
import plotly.express as px

st.title("AI Market Intelligence Dashboard")

file_path = "data/market_reaction.xlsx"

df = pd.read_excel(file_path)

df['date'] = pd.to_datetime(df['date'])

fig = px.line(
    df,
    x='date',
    y=['sp500', 'crude_oil_wti'],
    title='Market Trends'
)

st.plotly_chart(fig, width='stretch')

st.dataframe(df.head())

# Correct File Path
path = r"C:\Users\Yash\market_reaction.xlsx"

# Step 1 - Load Dataset
loader = IngestionAgent(path)
df = loader.load_data()

print("Dataset Loaded Successfully")
print(df.head())

# Step 2 - Market Analysis
analysis_agent = AnalysisAgent()
insights = analysis_agent.analyze(df)

print("\nMarket Insights:")
print(insights)

# Step 3 - Forecasting
forecast_agent = ForecastAgent()
forecast = forecast_agent.forecast_sp500(df)

print("\nForecast Generated")
print(forecast.head())

# Step 4 - Anomaly Detection
anomaly_agent = AnomalyAgent()
anomalies = anomaly_agent.detect(df)

print("\nAnomalies Detected:")
print(anomalies.head())

# Step 5 - RAG Memory Storage
rag_agent = RAGAgent()
rag_agent.build_memory(insights)

print("\nRAG Memory Created Successfully")

# Step 6 - Generate Report
report_agent = ReportAgent()
report = report_agent.generate(insights, anomalies)

print("\nFinal Report:")
print(report)

import streamlit as st

st.title("AI Market Intelligence Dashboard")
st.write("Deployment Successful 🚀")