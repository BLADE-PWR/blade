#!/usr/bin/env python3
"""Tables from the evaluator's CSVs (state/eval/eval_<wid>_<start>.csv)."""
from __future__ import annotations

import argparse
import csv
import glob
from collections import defaultdict

from blade_evaluator.evaluate import cep


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="+")
    ap.add_argument("--by", choices=["grp", "sid", "wid"], default="grp", help="split rows by this column too")
    args = ap.parse_args()
    files = [f for pat in args.csv for f in glob.glob(pat)]
    if not files:
        print("no CSV files match")
        return 1
    errs: dict[tuple, list[float]] = defaultdict(list)
    for f in files:
        with open(f) as fh:
            for r in csv.DictReader(fh):
                key = (r["level"], r["grp"]) if args.by == "grp" else (r["level"], r["grp"], r[args.by])
                errs[key].append(float(r["err_m"]))
    head = ["level", "group"] + ([args.by] if args.by != "grp" else []) + ["n", "CEP50 m", "CEP90 m", "mean m"]
    rows = []
    for key in sorted(errs):
        e = errs[key]
        c50, c90 = cep(e)
        rows.append([*key, str(len(e)), f"{c50:.2f}", f"{c90:.2f}", f"{sum(e) / len(e):.2f}"])
    print("| " + " | ".join(head) + " |")
    print("|" + "---|" * len(head))
    for r in rows:
        print("| " + " | ".join(r) + " |")
    print(f"\n{len(files)} file(s). geo = every georef point against truth in the same frame; "
          "world = world objects after fusion.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
