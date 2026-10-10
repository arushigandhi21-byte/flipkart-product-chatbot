from functools import lru_cache

import chromadb
from chromadb.utils import embedding_functions
import pandas
from config import APP_DIR, get_groq_client, get_model


@lru_cache(maxsize=1)
def get_embedding_function():
    return embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name='sentence-transformers/all-MiniLM-L6-v2'
    )

chroma_client = chromadb.Client()
collection_name_faq = 'faqs'
faqs_path = APP_DIR / "resources" / "faq_data.csv"


def ingest_faq_data(path):
    existing_names = [
        c if isinstance(c, str) else c.name
        for c in chroma_client.list_collections()
    ]
    if collection_name_faq not in existing_names:
        print("Ingesting FAQ data into Chromadb...")
        collection = chroma_client.create_collection(
            name=collection_name_faq,
            embedding_function=get_embedding_function()
        )
        df = pandas.read_csv(path)
        docs = df['question'].to_list()
        metadata = [{'answer': ans} for ans in df['answer'].to_list()]
        ids = [f"id_{i}" for i in range(len(docs))]
        collection.add(
            documents=docs,
            metadatas=metadata,
            ids=ids
        )
        print(f"FAQ Data successfully ingested into Chroma collection: {collection_name_faq}")
    else:
        print(f"Collection: {collection_name_faq} already exist")


def get_relevant_qa(query):
    collection = chroma_client.get_collection(
        name=collection_name_faq,
        embedding_function=get_embedding_function()
    )
    result = collection.query(
        query_texts=[query],
        n_results=2
    )
    return result


def generate_answer(query, context):
    prompt = f'''Given the following context and question, generate answer based on this context only.
    If the answer is not found in the context, kindly state "I don't know". Don't try to make up an answer.
    
    CONTEXT: {context}
    
    QUESTION: {query}
    '''
    completion = get_groq_client().chat.completions.create(
        model=get_model(),
        messages=[
            {
                'role': 'user',
                'content': prompt
            }
        ]
    )
    return completion.choices[0].message.content


def faq_chain(query):
    result = get_relevant_qa(query)
    context = "\n\n".join([r.get('answer', '') for r in result['metadatas'][0]])
    print("Context:", context)
    answer = generate_answer(query, context)
    return answer


if __name__ == '__main__':
    get_groq_client()  # Validate credentials before downloading the embedding model.
    ingest_faq_data(faqs_path)
    query = "what's your policy on defective products?"
    query = "Do you take cash as a payment option?"
    # result = get_relevant_qa(query)
    answer = faq_chain(query)
    print("Answer:",answer)
