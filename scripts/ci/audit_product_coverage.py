"""Static evidence-map check. No app startup, models or database access."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from app import product_maintenance


if __name__ == '__main__':
    result = product_maintenance.report()
    print(json.dumps(result, indent=2))
    raise SystemExit(bool(result['errors']))
