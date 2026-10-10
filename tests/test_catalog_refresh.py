import csv
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('catalog_importer', ROOT / 'web-scrapping/csv_to_sqlite.py')
importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(importer)


class CatalogRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.db = self.directory / 'catalog.sqlite'
        self.csv = self.directory / 'products.csv'
        self.rows = [dict(
            product_link=f'https://www.flipkart.com/shoe/p/item{i}?pid=SHOE{i}',
            title=f'Shoe {i}', brand='NIKE', price=500, discount=0.2,
            avg_rating=4.5, total_ratings=100,
        ) for i in range(12)]
        self.write_csv(self.rows)
        importer.import_products(self.csv, self.db)
        self.original = self.snapshot()

    def write_csv(self, rows):
        with self.csv.open('w', newline='') as output:
            writer = csv.DictWriter(output, fieldnames=importer.COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

    def snapshot(self, db=None):
        with sqlite3.connect(db or self.db) as conn:
            return conn.execute('SELECT * FROM product ORDER BY product_link').fetchall()

    def rejected(self, rows, **kwargs):
        self.write_csv(rows)
        with self.assertRaises(ValueError):
            importer.import_products(self.csv, self.db, **kwargs)
        self.assertEqual(self.snapshot(), self.original)
        self.assertFalse((self.directory / 'backups').exists())

    def test_success_backs_up_previous_data(self):
        self.write_csv([dict(row, price=600) for row in self.rows])
        self.assertEqual(importer.import_products(self.csv, self.db), 12)
        self.assertTrue(all(row[3] == 600 for row in self.snapshot()))
        backups = list((self.directory / 'backups').glob('*.sqlite'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.snapshot(backups[0]), self.original)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT old_rows,new_rows FROM catalog_refresh_history ORDER BY rowid DESC LIMIT 1').fetchone(), (12,12))

    def test_partial_scrape_preserves_database(self):
        self.rejected(self.rows[:2])

    def test_empty_scrape_preserves_database(self):
        self.rejected([])

    def test_changed_product_identities_preserve_database(self):
        self.rejected([dict(row, product_link=f'https://www.flipkart.com/shoe/p/new{i}?pid=OTHER{i}') for i,row in enumerate(self.rows)])

    def test_invalid_numeric_fields_preserve_database(self):
        for field,value in [('price',''),('price',-1),('discount',20),('avg_rating',8),('total_ratings',2.5),('price','inf')]:
            with self.subTest(field=field,value=value):
                self.rejected([dict(row, **{field:value}) for row in self.rows])

    def test_missing_rating_extraction_preserves_database(self):
        self.rejected([dict(row, avg_rating='') for row in self.rows])

    def test_bad_links_preserve_database(self):
        self.rejected([dict(row, product_link='https://www.flipkart.com/search?q=shoes') for row in self.rows])

    def manifest(self, **changes):
        manifest=dict(status='complete',failed_requests=0,attempted_products=12,successful_products=12,output_rows=12,csv_path=str(self.csv))
        manifest.update(changes)
        path=self.directory/'manifest.json'
        path.write_text(json.dumps(manifest))
        return path

    def test_failed_manifest_preserves_database(self):
        self.rejected(self.rows,manifest_path=self.manifest(status='failed',failed_requests=1))

    def test_partial_or_wrong_manifest_preserves_database(self):
        for changes in [dict(successful_products=11),dict(output_rows=11),dict(csv_path=str(self.directory/'other.csv'))]:
            with self.subTest(changes=changes):
                self.rejected(self.rows,manifest_path=self.manifest(**changes))

    def test_complete_manifest_is_accepted(self):
        self.assertEqual(importer.import_products(self.csv,self.db,manifest_path=self.manifest()),12)

    def test_database_write_failure_rolls_back_deleted_rows(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TRIGGER reject_price BEFORE INSERT ON product WHEN NEW.price > 1000 BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END")
        self.write_csv([dict(row,price=2000 if i==6 else 600) for i,row in enumerate(self.rows)])
        with self.assertRaises(sqlite3.IntegrityError):
            importer.import_products(self.csv,self.db)
        self.assertEqual(self.snapshot(),self.original)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM catalog_refresh_history').fetchone()[0],1)
        backup=next((self.directory/'backups').glob('*.sqlite'))
        self.assertEqual(self.snapshot(backup),self.original)

    def staged(self, rows, failed=None, **changes):
        failed = failed if failed is not None else self.rows[len(rows):]
        self.write_csv(rows)
        (self.directory/'failures.json').write_text(json.dumps([
            dict(product_link=row['product_link'],error='blocked') for row in failed
        ]))
        return self.manifest(status='partial' if failed else 'complete',
                             failed_requests=len(failed),successful_products=len(rows),
                             output_rows=len(rows),checked_products=12,
                             requested_urls=[r['product_link'] for r in self.rows],**changes)

    def test_partial_merge_preserves_failed_rows_and_records_dates(self):
        rows=[dict(row,price=600) for row in self.rows[:2]]
        summary=importer.merge_refresh(self.csv,self.db,manifest_path=self.staged(rows))
        self.assertEqual((summary['updated_products'],summary['retained_products']),(2,10))
        current=self.snapshot()
        for row in current:
            original=next(r for r in self.original if r[0]==row[0])
            self.assertEqual(row[3],600 if row[0] in [r['product_link'] for r in rows] else original[3])
        self.assertEqual(self.snapshot(summary['backup_path']),self.original)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM product_refresh_state WHERE last_success_at IS NULL AND status='retained'").fetchone()[0],10)
            first_date=conn.execute('SELECT last_success_at FROM product_refresh_state WHERE product_link=?',(rows[0]['product_link'],)).fetchone()[0]
        # A later failure preserves the last successful data and date.
        second=[dict(row,price=700) for row in self.rows[1:]]
        importer.merge_refresh(self.csv,self.db,manifest_path=self.staged(second,failed=self.rows[:1]))
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT last_success_at,status FROM product_refresh_state WHERE product_link=?',(rows[0]['product_link'],)).fetchone(),(first_date,'retained'))

    def test_invalid_products_are_retained_but_valid_ones_update(self):
        rows=[dict(row,price=600) for row in self.rows]
        rows[0]['avg_rating']=8
        rows[1]['discount']=''
        summary=importer.merge_refresh(self.csv,self.db,manifest_path=self.staged(rows))
        self.assertEqual((summary['updated_products'],summary['retained_products']),(10,2))
        for link in [r['product_link'] for r in rows[:2]]:
            self.assertEqual(next(r for r in self.snapshot() if r[0]==link),next(r for r in self.original if r[0]==link))

    def test_all_failed_or_invalid_preserves_entire_database(self):
        for rows in [[],[dict(row,price=-1) for row in self.rows]]:
            with self.assertRaises(ValueError):
                importer.merge_refresh(self.csv,self.db,manifest_path=self.staged(rows))
            self.assertEqual(self.snapshot(),self.original)
            self.assertFalse((self.directory/'backups').exists())

    def test_incomplete_or_unaccounted_scrape_rejected(self):
        manifest=self.staged(self.rows[:2])
        data=json.loads(manifest.read_text());data['checked_products']=2
        manifest.write_text(json.dumps(data))
        with self.assertRaises(ValueError): importer.merge_refresh(self.csv,self.db,manifest_path=manifest)
        manifest=self.staged(self.rows[:2],failed=self.rows[3:])
        with self.assertRaises(ValueError): importer.merge_refresh(self.csv,self.db,manifest_path=manifest)
        self.assertEqual(self.snapshot(),self.original)

    def test_merge_write_error_rolls_back_updates_and_metadata(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TRIGGER reject_update BEFORE UPDATE ON product WHEN NEW.price > 1000 BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END")
        rows=[dict(row,price=2000 if i==6 else 600) for i,row in enumerate(self.rows)]
        with self.assertRaises(sqlite3.IntegrityError):
            importer.merge_refresh(self.csv,self.db,manifest_path=self.staged(rows))
        self.assertEqual(self.snapshot(),self.original)
        with sqlite3.connect(self.db) as conn:
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='product_refresh_state'").fetchone())

    def test_scraper_output_publishes_partial_catalog(self):
        spec=importlib.util.spec_from_file_location('pipeline_scraper',ROOT/'web-scrapping/flipkart_scraper.py')
        scraper=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scraper)
        by_link={row['product_link']:row for row in self.rows}
        failed_link=self.rows[0]['product_link']
        def fetch(link):
            if link==failed_link:
                return None,dict(product_link=link,error='blocked')
            return dict(by_link[link],price=600),None
        with patch.object(scraper,'fetch_product',side_effect=fetch):
            csv_path,manifest=scraper.stage_products(list(by_link),self.directory/'run')
        result=importer.merge_refresh(csv_path,self.db,manifest_path=manifest)
        self.assertEqual((result['updated_products'],result['retained_products']),(11,1))
        self.assertEqual(next(row for row in self.snapshot() if row[0]==failed_link),self.original[0])
        self.assertEqual(json.loads((manifest.parent/'publication.json').read_text())['retained_products'],1)


if __name__ == '__main__':
    unittest.main()
