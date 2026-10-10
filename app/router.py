from functools import lru_cache
import config  # Set the project-local model cache before importing encoders.
from semantic_router import Route
from semantic_router.routers import SemanticRouter
from semantic_router.index import LocalIndex
from semantic_router.encoders import HuggingFaceEncoder

faq = Route(
    name='faq',
    utterances=[
        "What is the return policy of the products?",
        "Do I get discount with the HDFC credit card?",
        "How can I track my order?",
        "What payment methods are accepted?",
        "How long does it take to process a refund?",
    ]
)

sql = Route(
    name='sql',
    utterances=[
        "I want to buy nike shoes that have 50% discount.",
        "Are there any shoes under Rs. 3000?",
        "Do you have formal shoes in size 9?",
        "Are there any Puma shoes on sale?",
        "What is the price of puma running shoes?",
    ]
)

@lru_cache(maxsize=1)
def get_router():
    encoder = HuggingFaceEncoder(name="sentence-transformers/all-MiniLM-L6-v2")
    return SemanticRouter(
        routes=[faq, sql], encoder=encoder,
        index=LocalIndex(), auto_sync="local",
        aggregation="max",
    )


def router(query):
    return get_router()(query)

if __name__ == "__main__":
    print(router("What is your policy on defective product?").name)
    print(router("Pink Puma shoes in price range 5000 to 1000").name)
