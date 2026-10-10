import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('flipkart_scraper', ROOT / 'web-scrapping/flipkart_scraper.py')
scraper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scraper)
URL='https://www.flipkart.com/test/p/item?pid=SHOE1'


def html(product_id='SHOE1', context_id='SHOE1'):
    product=dict(sku=product_id,name='Example shoe',brand={'name':'NIKE'},offers={'price':800,'priceCurrency':'INR'},aggregateRating={'ratingValue':4.6,'ratingCount':120})
    product['@type']='Product'
    context=dict(productId=context_id,fdpEventTracking={'events':{'psi':{'ppd':{'mrp':1000}}}})
    state={'multiWidgetState':{'pageDataResponse':{'pageContext':context}}}
    recommendation=dict(product,sku='OTHER',offers={'price':100,'priceCurrency':'INR'})
    return '<script type="application/ld+json">'+json.dumps([recommendation,product])+'</script><script>window.__INITIAL_STATE__ = '+json.dumps(state)+';</script>'


class ScraperTests(unittest.TestCase):
    def test_primary_product_data_ignores_recommendations(self):
        row=scraper.parse_product(html(),URL)
        self.assertEqual(row['price'],800)
        self.assertEqual(row['discount'],0.2)
        self.assertEqual(row['avg_rating'],4.6)
        self.assertEqual(row['total_ratings'],120)
        self.assertEqual(row['product_link'],URL)

    def test_wrong_product_or_error_page_rejected(self):
        for page in [html(product_id='OTHER'),'<h1>Something went wrong E002</h1>']:
            with self.assertRaises(ValueError): scraper.parse_product(page,URL)

    def test_other_product_discount_never_used(self):
        self.assertIsNone(scraper.parse_product(html(context_id='OTHER'),URL)['discount'])

    def test_failure_does_not_stop_remaining_products(self):
        with tempfile.TemporaryDirectory() as temp:
            def fetch(url):
                if url == 'other':
                    return None, {'error':'blocked', 'product_link':url}
                return scraper.parse_product(html(),url), None
            with patch.object(scraper,'fetch_product',side_effect=fetch) as mock:
                csv_path,manifest_path=scraper.stage_products(['other', URL, URL],temp)
            manifest=json.loads(manifest_path.read_text())
            self.assertEqual(mock.call_count,3)
            self.assertEqual(manifest['status'],'partial')
            self.assertEqual(manifest['failed_requests'],1)
            self.assertEqual(manifest['attempted_products'],3)
            self.assertEqual(manifest['checked_products'],3)
            self.assertEqual(manifest['successful_products'],2)
            self.assertTrue(csv_path.exists())


if __name__=='__main__': unittest.main()
