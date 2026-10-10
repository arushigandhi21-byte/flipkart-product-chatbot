"""Run the staged scraper and publish validated updates while retaining failed products."""
from datetime import datetime, timezone
import json
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent


def main():
    runs = ROOT / 'web-scrapping/refresh-runs'
    runs.mkdir(exist_ok=True)
    with (runs / 'refresh.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another refresh is already running; this run was skipped.', file=sys.stderr)
            return 1
        return execute_refresh(runs)


def execute_refresh(runs):
    # A private kernel spec pins notebook execution to this Python environment.
    jupyter = ROOT / '.cache/jupyter'
    kernel = jupyter / 'kernels/catalog-refresh'
    kernel.mkdir(parents=True, exist_ok=True)
    (kernel / 'kernel.json').write_text(json.dumps({
        'argv': [sys.executable, '-m', 'ipykernel_launcher', '-f', '{connection_file}'],
        'display_name': 'Catalog refresh', 'language': 'python',
    }))
    env = os.environ.copy()
    env['JUPYTER_PATH'] = str(jupyter) + (os.pathsep + env['JUPYTER_PATH'] if env.get('JUPYTER_PATH') else '')
    env['JUPYTER_RUNTIME_DIR'] = str(jupyter / 'runtime')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    log = runs / f'execution-{stamp}.log'
    command = [
        sys.executable, '-m', 'nbconvert', '--execute', '--to', 'notebook',
        str(ROOT / 'web-scrapping/flipkart_data_extraction.ipynb'),
        '--ExecutePreprocessor.kernel_name=catalog-refresh',
        '--ExecutePreprocessor.timeout=1800',
        '--output', f'execution-{stamp}.ipynb', '--output-dir', str(runs),
    ]
    print(f'Running catalog refresh (failed products retain previous data). Log: {log}', flush=True)
    started_wall = time.time()
    with log.open('w') as output:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
        started = time.monotonic()
        while process.poll() is None:
            time.sleep(1)
            elapsed = int(time.monotonic() - started)
            if elapsed and elapsed % 30 == 0:
                progress_files = [p for p in runs.glob('scrape-*/progress.json') if p.stat().st_mtime >= started_wall]
                if progress_files:
                    try:
                        progress = json.loads(max(progress_files, key=lambda p: p.stat().st_mtime).read_text())
                        print(f"Refresh running: {progress['checked_products']}/{progress['total_products']} checked, {progress['failed_products']} failed", flush=True)
                    except (OSError, json.JSONDecodeError):
                        print(f'Refresh running ({elapsed}s); waiting for the next progress update.', flush=True)
                else:
                    print(f'Refresh running ({elapsed}s); preparing or loading product pages.', flush=True)
    if process.returncode:
        failures = [p for p in runs.glob('scrape-*/failures.json') if p.stat().st_mtime >= started_wall]
        if failures:
            report = max(failures, key=lambda p: p.stat().st_mtime)
            try:
                entries = json.loads(report.read_text())
                if entries:
                    print(f"Product extraction failed: {entries[0]['error']}", file=sys.stderr)
                    print(f'Failure details: {report}', file=sys.stderr)
            except (OSError, json.JSONDecodeError, KeyError):
                pass
        print(f'Refresh command failed. Scraping or validation failures preserve the previous catalog; see {log} for the publication status.', file=sys.stderr)
    else:
        reports = [p for p in runs.glob('scrape-*/publication.json') if p.stat().st_mtime >= started_wall]
        if not reports:
            print(f'No publication report found; inspect {log} before assuming the refresh succeeded.', file=sys.stderr)
            return 1
        report = max(reports, key=lambda p: p.stat().st_mtime)
        summary = json.loads(report.read_text())
        print(f"Refresh published: {summary['updated_products']} products updated; "
              f"{summary['retained_products']} retained previous data. "
              "New chatbot queries use the saved catalog automatically.")
        print(f'Refresh dates and failure details: {report}')
    return process.returncode


if __name__ == '__main__':
    sys.exit(main())
