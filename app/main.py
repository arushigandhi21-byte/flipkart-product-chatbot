import streamlit as st
from faq import ingest_faq_data, faq_chain
from sql import sql_chain
from pathlib import Path
from router import router
from config import credentials_configured
from groq import GroqError
import sqlite3
import pandas as pd

faqs_path = Path(__file__).parent / "resources/faq_data.csv"


def ask(query):
    route = router(query).name
    if route == 'faq':
        return faq_chain(query)
    elif route == 'sql':
        return sql_chain(query)
    else:
        return "Please ask about our policies or search for shoes by brand, price, or rating."

st.title("E-commerce Bot")
if not credentials_configured():
    st.info("Add your Groq API key to app/.env, then restart the app. See README.md for setup instructions.")
    st.stop()

try:
    ingest_faq_data(faqs_path)
except Exception as exc:
    st.error(f"Could not load FAQ data or the embedding model: {exc}")
    st.stop()

query = st.chat_input("Write your query")

if "messages" not in st.session_state:
    st.session_state["messages"] = []

for message in st.session_state.messages:
    with st.chat_message(message['role']):
        st.markdown(message['content'])

if query:
    with st.chat_message("user"):
        st.markdown(query)
    st.session_state.messages.append({"role":"user", "content":query})

    try:
        with st.spinner("Finding an answer..."):
            response = ask(query)
    except (GroqError, ValueError, sqlite3.Error, pd.errors.DatabaseError) as exc:
        st.error(f"Could not answer this question: {exc}")
        st.stop()
    with st.chat_message("assistant"):
        st.markdown(response)
    st.session_state.messages.append({"role": "assistant", "content": response})

