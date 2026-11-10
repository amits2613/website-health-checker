# Website Health Checker

An agent that checks whether a website is healthy. It runs about a dozen
checks in a few seconds and prints a report. It uses only the Python standard
library, so it has no packages to install, no headless browser, and no paid APIs.

- [architecture.md](architecture.md): what "healthy" means here, the check flow, and the design decisions.

## What is checked

| Group | What it looks for | Result when it fails |
|---|---|---|
| Reachable | The site answers with a 2xx status | **fail** |
| Redirects | No loops, no more than 5 hops, http moves to https | loop: **fail**; http stays http: warn |
| Response time | Homepage responds within 1 second | warn |
| TLS certificate | Certificate is valid; days until expiry | under 7 days: **fail**; under 30: warn |
| Security headers | HSTS, CSP, X-Content-Type-Options, X-Frame-Options | warn |
| Compression and caching | HTML is gzip-compressed; Cache-Control is set | warn |
| Page basics | `<title>`, meta description, `lang`, exactly one `<h1>` | warn |
| Mixed content | No http:// images or scripts on an https page | warn |
| Internal links | Links on the homepage (up to 30) do not return errors | **fail** |
| robots.txt, sitemap.xml | Both files exist | warn |

Only a **fail** makes the site "not healthy". Warnings are advice.

## Code layout

```
src/
├── checker.py              # all the checks, plus fetching and HTML reading
├── main.py                 # command-line entry point
└── tests/
    └── test_checker.py     # 12 tests, using a local test web server
```

`checker.py` in four parts:

| Part | Functions | What it does |
|---|---|---|
| Fetching | `fetch`, `follow_redirects` | Gets a URL, follows redirects by hand so they can be counted |
| Reading HTML | `PageParser`, `parse_page` | Pulls out the title, description, links, and resources |
| Rules | `tls_status`, `timing_status` | Turns numbers into pass, warn, or fail. Easy to test |
| Checks and summary | `check_*` functions, `check_site`, `_summary` | Runs every check and counts the results |

## Requirements

- Python 3.9 or newer. Nothing else.

## How to run locally

From the repository root:

```bash
# Show the help
python3 src/main.py --help

# Check a site (needs internet access)
python3 src/main.py https://github.com

# Same check, as JSON
python3 src/main.py https://github.com --json

# Check fewer homepage links, for a faster run
python3 src/main.py https://github.com --max-links 10

# Run the tests (no internet needed)
python3 -m unittest discover -s src/tests -v
```

### Options

| Option | Meaning | Default |
|---|---|---|
| `url` | Full URL, starting with http:// or https:// | required |
| `--json` | Print the report as JSON | off |
| `--max-links` | How many homepage links to check | 30 |
| `--timeout` | Seconds to wait for each request | 10 |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Healthy: no failed checks (warnings are allowed) |
| 1 | Not healthy: at least one check failed |
| 2 | Bad input, such as a URL without http:// or https:// |

## Example output

```
Health report for https://github.com (2026-10-16T10:12:26+00:00)

  [PASS] Reachable: 200 from https://github.com
  [PASS] Redirects: 0 redirect(s)
  [PASS] Response time: 0.07s
  [WARN] TLS certificate: expires in 29 days
  ...
HEALTHY: 9 passed, 2 warnings, 0 failed
```

## Tuning

Thresholds are constants at the top of `src/checker.py`:

- `SLOW_SECONDS` (default 1.0): response time that triggers a warning.
- `TLS_WARN_DAYS` (30) and `TLS_FAIL_DAYS` (7): certificate expiry limits.
- `MAX_REDIRECTS` (5): redirect limit.
- `MAX_BODY_BYTES` (2 MB): page content read per request.

## Troubleshooting

- **"could not connect" for a site you know is up**: check your network or proxy, and confirm the URL spelling.
- **Many warnings about missing headers**: these are advice, not failures. Only failures change the exit code.
- **Slow run**: lower `--max-links`. Links are checked in parallel with 8 workers.
