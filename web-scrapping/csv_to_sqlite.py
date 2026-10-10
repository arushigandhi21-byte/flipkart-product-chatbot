"""Validate and atomically refresh a catalog, preserving the last good database."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent
COLUMNS = ['product_link', 'title', 'brand', 'price', 'discount', 'avg_rating', 'total_ratings']


def product_id(url):
    parsed = urlsplit(str(url))
    pid = parse_qs(parsed.query).get('pid', [''])[0]
    if parsed.scheme != 'https' or parsed.hostname not in {'flipkart.com', 'www.flipkart.com'} or '/p/' not in parsed.path or not pid:
        raise ValueError('Invalid Flipkart product URL or missing product ID.')
    return pid


def validate_csv(csv_path, manifest_path=None):
    csv_path = Path(csv_path).resolve()
    if manifest_path is not None:
        manifest = json.loads(Path(manifest_path).read_text())
        if manifest.get('status') != 'complete' or manifest.get('failed_requests') != 0:
            raise ValueError('Scrape failed or was incomplete; previous database preserved.')
        if Path(manifest.get('csv_path', '')).resolve() != csv_path:
            raise ValueError('Scrape report does not describe this CSV.')
        attempted = manifest.get('attempted_products')
        successful = manifest.get('successful_products')
        if not isinstance(attempted, int) or attempted <= 0 or successful != attempted:
            raise ValueError('Not all requested products were successfully extracted.')
    df = pd.read_csv(csv_path)
    if manifest_path is not None and manifest.get('output_rows') != len(df):
        raise ValueError('CSV row count does not match the scrape report.')
    return validate_frame(df).drop_duplicates(subset=['product_link'])


def validate_frame(df):
    df = df.copy()
    missing = set(COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"CSV missing columns: {', '.join(sorted(missing))}")
    if df.empty:
        raise ValueError('CSV contains no products; previous database preserved.')
    for name in ['product_link', 'title', 'brand']:
        if df[name].isna().any() or df[name].astype(str).str.strip().eq('').any():
            raise ValueError(f'Blank required field: {name}')
    for url in df['product_link']:
        product_id(url)
    for name in ['price', 'discount', 'avg_rating', 'total_ratings']:
        df[name] = pd.to_numeric(df[name], errors='raise')
        if any(not math.isfinite(float(x)) for x in df[name].dropna()):
            raise ValueError(f'Non-finite value in {name}')
    if df['price'].isna().any() or (df['price'] <= 0).any() or (df['price'] % 1 != 0).any():
        raise ValueError('Prices must be positive whole rupee amounts.')
    for name, low, high in [('discount', 0, 1), ('avg_rating', 0, 5)]:
        values = df[name].dropna()
        if not values.between(low, high).all():
            raise ValueError(f'{name} must be between {low} and {high}.')
    ratings = df['total_ratings'].dropna()
    if (ratings < 0).any() or (ratings % 1 != 0).any():
        raise ValueError('Rating counts must be nonnegative whole numbers.')
    return df[COLUMNS]


def merge_refresh(csv_path, db_path, *, manifest_path):
    """Update existing links only; retain whole records for failed/invalid products.

    A complete accounting of every requested URL is required before publication.
    Dates live separately so the chatbot's product schema and formatting stay intact.
    """
    csv_path = Path(csv_path).resolve()
    db_path = Path(db_path).resolve()
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text())
    failures = json.loads((manifest_path.parent / 'failures.json').read_text())
    df = pd.read_csv(csv_path)
    requested = manifest.get('requested_urls', [])
    if (manifest.get('status') not in {'complete', 'partial'}
            or not requested or not all(isinstance(url, str) for url in requested)
            or manifest.get('attempted_products') != len(requested)
            or manifest.get('checked_products') != len(requested)
            or manifest.get('successful_products') != len(df)
            or manifest.get('output_rows') != len(df)
            or manifest.get('failed_requests') != len(failures)
            or Path(manifest.get('csv_path', '')).resolve() != csv_path
            or set(COLUMNS) - set(df.columns)):
        raise ValueError('Incomplete or inconsistent scrape report; previous catalog preserved.')
    if Counter(df['product_link'].tolist() + [item['product_link'] for item in failures]) != Counter(requested):
        raise ValueError('Scrape results do not account for every requested URL.')
    stamp = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute('BEGIN IMMEDIATE')
        old = pd.read_sql_query('SELECT * FROM product', conn)
        if Counter(old['product_link']) != Counter(requested):
            raise ValueError('Catalog changed during scraping; retry with the current catalog.')
        valid = []
        for _, row in df.iterrows():
            try:
                clean = validate_frame(pd.DataFrame([row]))
                previous = old[old['product_link'] == row['product_link']]
                for field in ['discount', 'avg_rating', 'total_ratings']:
                    if clean[field].isna().any() and previous[field].notna().any():
                        raise ValueError(f'Previously available {field} is missing.')
                valid.append(clean.iloc[0])
            except (ValueError, TypeError) as exc:
                failures.append({'product_link': row['product_link'], 'error': str(exc)})
        # Conflicting observations of one link cannot overwrite each other.
        updates = {}
        rejected = {item['product_link'] for item in failures}
        for row in valid:
            # sqlite3 treats NumPy integer scalars as blobs; use native scalars.
            values = tuple(None if pd.isna(row[name]) else
                           row[name].item() if hasattr(row[name], 'item') else row[name]
                           for name in COLUMNS)
            link = row['product_link']
            if link in updates and updates[link] != values:
                rejected.add(link)
                failures.append({'product_link': link, 'error': 'Conflicting product observations.'})
            updates[link] = values
        for link in rejected:
            updates.pop(link, None)
        updated_rows = sum(link in updates for link in old['product_link'])
        if not updated_rows:
            raise ValueError('No valid products were fetched; previous catalog preserved.')
        backup_dir = db_path.parent / 'backups'
        backup_dir.mkdir(exist_ok=True)
        backup_path = backup_dir / f'{db_path.stem}-{uuid4().hex}.sqlite'
        with sqlite3.connect(f'{db_path.as_uri()}?mode=ro', uri=True) as source, sqlite3.connect(backup_path) as destination:
            source.backup(destination)
        conn.execute('''CREATE TABLE IF NOT EXISTS product_refresh_state (
            product_link TEXT PRIMARY KEY, last_success_at TEXT,
            last_attempt_at TEXT, status TEXT, error TEXT
        )''')
        errors = {item['product_link']: item['error'] for item in failures}
        for link in set(requested):
            if link in updates:
                values = updates[link]
                conn.execute('''UPDATE product SET title=?, brand=?, price=?,
                    discount=?, avg_rating=?, total_ratings=? WHERE product_link=?''', values[1:] + (link,))
            conn.execute('''INSERT INTO product_refresh_state VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(product_link) DO UPDATE SET
                last_success_at=COALESCE(excluded.last_success_at, product_refresh_state.last_success_at),
                last_attempt_at=excluded.last_attempt_at, status=excluded.status, error=excluded.error''',
                (link, stamp if link in updates else None, stamp,
                 'updated' if link in updates else 'retained', errors.get(link)))
        summary = dict(status='partial' if updated_rows < len(old) else 'complete',
                       refreshed_at=stamp, updated_products=updated_rows,
                       retained_products=len(old)-updated_rows, total_products=len(old),
                       backup_path=str(backup_path), failures=failures)
        conn.execute('''CREATE TABLE IF NOT EXISTS catalog_refresh_runs (
            refreshed_at TEXT, source_csv TEXT, updated_products INTEGER,
            retained_products INTEGER, total_products INTEGER, backup_path TEXT
        )''')
        conn.execute('INSERT INTO catalog_refresh_runs VALUES (?, ?, ?, ?, ?, ?)',
                     (stamp, str(csv_path), updated_rows, len(old)-updated_rows, len(old), str(backup_path)))
    (manifest_path.parent / 'publication.json').write_text(json.dumps(summary, indent=2))
    return summary


def import_products(csv_path, db_path, *, manifest_path=None, min_coverage=0.9):
    """Reject unsafe inputs; back up and replace rows within one transaction.

    Coverage checks protect both row count and identities, plus rating-field coverage.
    No option bypasses those checks for a populated database.
    """
    if not 0 < min_coverage <= 1:
        raise ValueError('min_coverage must be greater than zero and at most one.')
    df = validate_csv(csv_path, manifest_path)
    db_path = Path(db_path).resolve()
    records = df.astype(object).where(df.notna(), None).values.tolist()
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute('BEGIN IMMEDIATE')
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='product'").fetchone()
        if exists:
            old = pd.read_sql_query('SELECT * FROM product', conn)
            if len(df) < len(old) * min_coverage:
                raise ValueError(f'Only {len(df)} rows for an existing {len(old)}-row catalog; refresh rejected.')
            old_ids = {product_id(url) for url in old['product_link']}
            new_ids = {product_id(url) for url in df['product_link']}
            if old_ids and len(old_ids & new_ids) < len(old_ids) * min_coverage:
                raise ValueError('Too many existing product IDs are missing; refresh rejected.')
            for field in ['discount', 'avg_rating', 'total_ratings']:
                if df[field].notna().sum() < old[field].notna().sum() * min_coverage:
                    raise ValueError(f'Too much {field} data is missing; refresh rejected.')
        else:
            old = pd.DataFrame(columns=COLUMNS)
        backup_path = None
        if exists:
            backup_dir = db_path.parent / 'backups'
            backup_dir.mkdir(exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup_path = backup_dir / f'{db_path.stem}-{stamp}-{uuid4().hex[:8]}.sqlite'
            # A separate read connection can back up the old snapshot while our
            # reserved write lock prevents a competing writer from changing it.
            with sqlite3.connect(f'{db_path.as_uri()}?mode=ro', uri=True) as source, sqlite3.connect(backup_path) as destination:
                source.backup(destination)
        conn.execute('''CREATE TABLE IF NOT EXISTS product (
            product_link TEXT, title TEXT, brand TEXT, price INTEGER,
            discount REAL, avg_rating REAL, total_ratings INTEGER
        )''')
        conn.execute('DELETE FROM product')
        conn.executemany('''INSERT INTO product
            (product_link, title, brand, price, discount, avg_rating, total_ratings)
            VALUES (?, ?, ?, ?, ?, ?, ?)''', records)
        conn.execute('''CREATE TABLE IF NOT EXISTS catalog_refresh_history (
            refreshed_at TEXT, source_csv TEXT, old_rows INTEGER,
            new_rows INTEGER, backup_path TEXT
        )''')
        conn.execute('INSERT INTO catalog_refresh_history VALUES (?, ?, ?, ?, ?)', (
            datetime.now(timezone.utc).isoformat(), str(Path(csv_path).resolve()),
            len(old), len(df), str(backup_path) if backup_path else None,
        ))
        # Context manager commits all changes together, or rolls them all back.
    return len(df)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv', type=Path, default=PROJECT_DIR / 'web-scrapping/flipkart_product_data.csv')
    parser.add_argument('--db', type=Path, default=PROJECT_DIR / 'app/db.sqlite')
    parser.add_argument('--manifest', type=Path)
    args = parser.parse_args()
    try:
        count = import_products(args.csv, args.db, manifest_path=args.manifest)
    except (ValueError, OSError, sqlite3.Error, pd.errors.ParserError) as exc:
        parser.exit(1, f'Refresh rejected; previous database preserved: {exc}\n')
    print(f'Refreshed {count} products. Previous database backed up in {args.db.parent / "backups"}.')
