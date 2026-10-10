"""Check model availability; --live tests both chatbot chains using your Groq key."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from config import get_groq_client, get_model


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Make three chat completion requests to test FAQ and SQL answers.")
    args = parser.parse_args()
    client = get_groq_client()
    models = sorted(model.id for model in client.models.list().data)
    model = get_model()
    if model not in models:
        print(f"Configured model {model!r} is not listed. Models reported by Groq:")
        print("\n".join(models))
        sys.exit(1)
    print(f"Configured model listed by Groq: {model}")
    if args.live:
        from faq import faq_chain, faqs_path, ingest_faq_data
        from sql import sql_chain
        ingest_faq_data(faqs_path)
        answer = faq_chain("What is your return policy?")
        assert answer and answer.strip(), "FAQ returned no answer"
        print("FAQ live answer:", answer)
        answer = sql_chain("Show me the top 3 shoes by average rating.")
        assert answer and answer.strip() and not answer.startswith("Sorry,"), "SQL chain did not complete"
        print("Product live answer:", answer)
