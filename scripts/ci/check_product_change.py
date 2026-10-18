"""Check affected product sources against source-bound surface review receipts."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2] / 'src'))
from app import product_maintenance


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base')
    args=parser.parse_args()
    receipt=json.loads((product_maintenance.ROOT/'docs/product-change-reviews.json').read_text(encoding='utf-8'))
    changed=[]
    if args.base and set(args.base)!={'0'}:
        # argv, no shell interpolation; CI supplies its base SHA.
        subprocess.run(['git','rev-parse','--verify',args.base+'^{commit}'],check=True,capture_output=True)
        changed=subprocess.run(['git','diff','--name-only',args.base,'HEAD','--','src/app'],check=True,capture_output=True,text=True).stdout.splitlines()
    result=product_maintenance.report()
    result['errors'].extend(product_maintenance.check_change(changed,receipt))
    result['errors']=sorted(set(result['errors']))
    print(json.dumps(result,indent=2))
    return bool(result['errors'])


if __name__=='__main__':raise SystemExit(main())
