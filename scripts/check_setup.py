"""Validate local setup without making Groq API calls.

Use --models to also check the downloaded embedding model and routing.
"""
import argparse
import ast
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import shutil

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", action="store_true")
    args = parser.parse_args()
    for folder in [ROOT / "app", ROOT / "scripts", ROOT / "web-scrapping"]:
        for path in folder.glob("*.py"):
            ast.parse(path.read_text(), filename=str(path))
    notebook = json.loads((ROOT / "web-scrapping/flipkart_data_extraction.ipynb").read_text())
    for cell in notebook["cells"]:
        if cell["cell_type"] == "code":
            ast.parse("".join(cell["source"]))
    print("Python and notebook syntax: OK")

    import config
    import faq
    import router
    import sql
    import selenium
    import bs4
    import lxml
    import ipykernel
    print("Chatbot and notebook imports: OK")
    products = sql.run_query("SELECT * FROM product")
    assert products is not None and len(products) > 0
    print(f"Product database: OK ({len(products)} products)")

    spec = importlib.util.spec_from_file_location("csv_to_sqlite", ROOT / "web-scrapping/csv_to_sqlite.py")
    importer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(importer)
    with tempfile.TemporaryDirectory() as temp:
        db = Path(temp) / "products.sqlite"
        shutil.copy2(ROOT / "app/db.sqlite", db)
        csv = ROOT / "app/resources/ecommerce_data_final.csv"
        count = importer.import_products(csv, db)
        assert importer.import_products(csv, db) == count
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM product").fetchone()[0] == count
        original_db = sql.db_path
        try:
            sql.db_path = db
            try:
                sql.run_query("SELECT 1; DELETE FROM product")
            except Exception:
                pass
            with sqlite3.connect(db) as conn:
                assert conn.execute("SELECT COUNT(*) FROM product").fetchone()[0] == count
        finally:
            sql.db_path = original_db
    print("CSV import and repeated import into temporary database: OK")

    if args.models:
        faq.ingest_faq_data(faq.faqs_path)
        faq.ingest_faq_data(faq.faqs_path)
        result = faq.get_relevant_qa("What is your return policy?")
        assert result["metadatas"][0] and "answer" in result["metadatas"][0][0]
        for query, expected in [
            ("What is the return policy of the products?", "faq"),
            ("Are there any shoes under Rs. 3000?", "sql"),
        ]:
            actual = router.router(query).name
            assert actual == expected, (query, expected, actual)
        print("FAQ retrieval and FAQ/product routing: OK")
    print("Groq API calls were not made; add a valid key to app/.env for live answers.")


if __name__ == "__main__":
    main()
