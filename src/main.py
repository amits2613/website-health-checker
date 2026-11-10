"""
Command-line entry point.

  python src/main.py https://example.com
  python src/main.py https://example.com --json
  python src/main.py https://example.com --max-links 50

Exit code: 0 if the site is healthy (no failed checks), 1 if not, 2 for bad input.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import checker  # noqa: E402

SYMBOLS = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}


def print_report(report):
    print(f"Health report for {report['url']} ({report['checked_at']})")
    print()
    for check in report["checks"]:
        print(f"  [{SYMBOLS[check['status']]}] {check['name']}: {check['detail']}")
    counts = report["counts"]
    print()
    verdict = "HEALTHY" if report["healthy"] else "NOT HEALTHY"
    print(f"{verdict}: {counts['pass']} passed, {counts['warn']} warnings, {counts['fail']} failed")


def main():
    parser = argparse.ArgumentParser(description="Check whether a website is healthy.")
    parser.add_argument("url", help="Full URL, for example https://example.com")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON.")
    parser.add_argument("--max-links", type=int, default=30,
                        help="How many homepage links to check (default 30).")
    parser.add_argument("--timeout", type=float, default=10,
                        help="Seconds to wait for each request (default 10).")
    args = parser.parse_args()

    try:
        report = checker.check_site(args.url, max_links=args.max_links, timeout=args.timeout)
    except ValueError as error:
        parser.error(str(error))
        return 2

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
