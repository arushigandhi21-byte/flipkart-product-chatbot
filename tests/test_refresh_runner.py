import fcntl
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('refresh_runner', ROOT / 'scripts/refresh_catalog.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RefreshRunnerTests(unittest.TestCase):
    def test_overlapping_refresh_is_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runs = root / 'web-scrapping/refresh-runs'
            runs.mkdir(parents=True)
            with (runs / 'refresh.lock').open('a') as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(runner, 'ROOT', root), patch.object(runner, 'execute_refresh') as execute:
                    self.assertEqual(runner.main(), 1)
                    execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
