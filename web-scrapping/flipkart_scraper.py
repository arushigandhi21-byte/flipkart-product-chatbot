"""Read structured metadata for the existing catalog; stage incomplete runs safely."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import time
import threading
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
import pandas as pd
import requests

COLUMNS = ['product_link', 'title', 'brand', 'price', 'discount', 'avg_rating', 'total_ratings']
_clients = threading.local()


def objects(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from objects(item)


def parse_product(html, url):
    pid = parse_qs(urlsplit(url).query).get('pid', [''])[0]
    soup = BeautifulSoup(html, 'html.parser')
    product = None
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.get_text())
        except json.JSONDecodeError:
            continue
        for item in objects(data):
            if item.get('@type') == 'Product' and item.get('sku') == pid:
                product = item
                break
        if product:
            break
    if product is None:
        raise ValueError('No structured metadata for the requested product ID; page may be blocked or unavailable.')
    offer = product.get('offers', {})
    if isinstance(offer, list):
        if len(offer) != 1:
            raise ValueError('Multiple offers have no unambiguous catalog price.')
        offer = offer[0]
    if offer.get('priceCurrency') != 'INR' or offer.get('price') is None:
        raise ValueError('Product has no INR price.')
    price = float(offer['price'])
    brand = product.get('brand', {})
    if isinstance(brand, dict):
        brand = brand.get('name')
    if not product.get('name') or not brand:
        raise ValueError('Product name or brand is missing.')
    discount = None
    for script in soup.find_all('script'):
        text = script.get_text().strip()
        if not text.startswith('window.__INITIAL_STATE__'):
            continue
        try:
            state, _ = json.JSONDecoder().raw_decode(text.split('=', 1)[1].lstrip())
            context = state['multiWidgetState']['pageDataResponse']['pageContext']
            if context.get('productId') != pid:
                continue
            pricing = context['fdpEventTracking']['events']['psi']['ppd']
            mrp = float(pricing['mrp'])
            if mrp > 0 and mrp >= price:
                discount = round(1 - price / mrp, 4)
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            pass
    aggregate = product.get('aggregateRating') or {}
    return dict(
        product_link=url, title=product['name'], brand=brand, price=price,
        discount=discount, avg_rating=aggregate.get('ratingValue'),
        total_ratings=aggregate.get('ratingCount'),
    )


def fetch_product(url):
    error = None
    for attempt in range(2):
        try:
            if not hasattr(_clients, 'session'):
                _clients.session = requests.Session()
            with _clients.session.get(url, timeout=(10, 10), stream=True) as response:
                response.raise_for_status()
                started = time.monotonic()
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > 4 * 1024 * 1024 or time.monotonic() - started > 30:
                        raise ValueError('Product page exceeded the size or time limit.')
                return parse_product(b''.join(chunks), url), None
        except (requests.RequestException, ValueError, TypeError) as exc:
            error = str(exc)
            if attempt == 0:
                time.sleep(1)
    return None, {'product_link': url, 'error': error}


def stage_products(urls, run_dir):
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    rows, failures = [], []
    checked = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fetch_product, url) for url in urls]
        for future in as_completed(futures):
            row, failure = future.result()
            checked += 1
            if failure:
                failures.append(failure)
                print(f"Keeping previous product data: {failure['error']}", flush=True)
            else:
                rows.append(row)
            if checked % 25 == 0 or checked == len(urls):
                (run_dir / 'progress.json').write_text(json.dumps({
                    'checked_products': checked, 'total_products': len(urls),
                    'failed_products': len(failures),
                }, indent=2))
                print(f'Checked {checked}/{len(urls)} products; extraction failures: {len(failures)}', flush=True)
    csv_path = run_dir / 'flipkart_product_data.csv'
    pd.DataFrame(rows, columns=COLUMNS).to_csv(csv_path, index=False)
    (run_dir / 'failures.json').write_text(json.dumps(failures, indent=2))
    manifest = dict(
        status='complete' if not failures else 'partial', failed_requests=len(failures),
        attempted_products=len(urls), checked_products=checked,
        requested_urls=list(urls), successful_products=len(rows), output_rows=len(rows),
        csv_path=str(csv_path.resolve()),
    )
    manifest_path = run_dir / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return csv_path, manifest_path
