import csv
import os
import sys
import glob
from typing import Any, Dict, List, Set

import yaml


REQUIRED_DD_VARS: Set[str] = {
    "DD_AGENT_HOST",
    "DD_SERVICE",
    "DD_VERSION",
    "DD_ENV",
}



def load_allowlist(csv_path: str) -> Dict[str, Set[str]]:
    allowlist: Dict[str, Set[str]] = {}
    if not os.path.isfile(csv_path):
        return allowlist

    with open(csv_path, newline="") as f:
        reader = csv.reader(f)
        header_consumed = False
        for row in reader:
            if not row:
                continue
            # Skip header row (expects first column to be 'repo')
            if not header_consumed:
                header_consumed = True
                continue
            repo = row[0].strip()
            flags = {cell.strip() for cell in row[1:] if cell and cell.strip()}
            if repo:
                allowlist[repo] = flags
    return allowlist


def repo_name_from_github_env() -> str:
    # GITHUB_REPOSITORY is in form "owner/repo"
    repo_full = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if "/" in repo_full:
        return repo_full.split("/", 1)[1]
    return repo_full or ""


def find_env_entries(obj: Any) -> List[Dict[str, Any]]:
    """
    Recursively find lists under key 'env' and return collected entries.
    Each entry is expected to be a dict with at least a 'name' key.
    """
    results: List[Dict[str, Any]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "env" and isinstance(v, list):
                for item in v:
                    if isinstance(item, dict) and "name" in item:
                        results.append(item)
            results.extend(find_env_entries(v))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(find_env_entries(item))
    return results




def collect_dd_flags(values_data: Any) -> Dict[str, Dict[str, Any]]:
    dd_flags: Dict[str, Dict[str, Any]] = {}
    for entry in find_env_entries(values_data):
        name = str(entry.get("name", "")).strip()
        if not name.startswith("DD_"):
            continue
        dd_flags[name] = entry
    return dd_flags


def load_yaml(file_path: str) -> Any:
    with open(file_path) as f:
        return yaml.safe_load(f)


def collect_dd_flags_from_file(file_path: str) -> Dict[str, Dict[str, Any]]:
    data = load_yaml(file_path)
    return collect_dd_flags(data)


def validate_optional_flags(file_path: str, repo: str, allowed_optional_flags: Set[str]) -> List[str]:
    violations: List[str] = []
    try:
        dd_flags = collect_dd_flags_from_file(file_path)
    except Exception as e:
        violations.append(f"{file_path}: Unable to parse YAML: {e}")
        return violations

    for flag_name in sorted(dd_flags.keys()):
        if flag_name in REQUIRED_DD_VARS:
            continue
        if flag_name not in allowed_optional_flags:
            violations.append(
                f"{file_path}: {flag_name} is not permitted for repo '{repo}'. Remove it or request approval."
            )

    return violations


def main() -> None:
    action_dir = os.path.dirname(os.path.abspath(__file__))
    allowlist_path = os.path.join(action_dir, "dd_flags_allowlist.csv")

    allowlist = load_allowlist(allowlist_path)
    repo = repo_name_from_github_env()
    if repo not in allowlist:
        print("❌ Repository is not registered for Datadog flags compliance checks.")
        print(f"  - Detected repo: '{repo or '[unknown]'}'")
        print("  - Action: Please contact the DevOps team to get this repository registered in the central allowlist (dd_flags_allowlist.csv).")
        print("  - Once registered, re-run the workflow.")
        sys.exit(1)
    allowed_optional_flags = allowlist.get(repo, set())

    # Look for values files only under charts/*/
    charts_info: Dict[str, Dict[str, str]] = {}
    for chart_dir in glob.glob(os.path.join(".", "charts", "*")):
        if not os.path.isdir(chart_dir):
            continue
        base_path = os.path.join(chart_dir, "values.yaml")
        prod_path = os.path.join(chart_dir, "values-production-us-central1.yaml")
        entry: Dict[str, str] = {}
        if os.path.isfile(base_path):
            entry["base"] = base_path
        if os.path.isfile(prod_path):
            entry["prod"] = prod_path
        if entry:
            charts_info[chart_dir] = entry

    if not charts_info:
        print("No values files found to validate under ./charts/*/. Skipping Datadog flags guard.")
        return

    all_violations: List[str] = []
    # First, validate disallowed optional flags per file
    for chart_dir, files in sorted(charts_info.items()):
        for fpath in sorted(files.values()):
            all_violations.extend(
                validate_optional_flags(fpath, repo, allowed_optional_flags)
            )

    # Next, validate required flags presence with precedence: prod > base
    for chart_dir, files in sorted(charts_info.items()):
        prod_path = files.get("prod")
        base_path = files.get("base")

        prod_flags: Set[str] = set()
        base_flags: Set[str] = set()
        prod_parse_error: str = ""
        base_parse_error: str = ""

        if prod_path:
            try:
                prod_flags = set(collect_dd_flags_from_file(prod_path).keys())
            except Exception as e:
                prod_parse_error = f"{prod_path}: Unable to parse YAML: {e}"
        if base_path:
            try:
                base_flags = set(collect_dd_flags_from_file(base_path).keys())
            except Exception as e:
                base_parse_error = f"{base_path}: Unable to parse YAML: {e}"

        # If there are parse errors, report and skip presence checks for that file
        if prod_parse_error:
            all_violations.append(prod_parse_error)
        if base_parse_error:
            all_violations.append(base_parse_error)

        # Determine missing required with precedence
        missing_for_chart: List[str] = []
        for var in sorted(REQUIRED_DD_VARS):
            if var in prod_flags:
                continue  # satisfied by prod
            if var in base_flags:
                continue  # satisfied by base when prod missing
            missing_for_chart.append(var)

        if missing_for_chart:
            # Attribute the error to values.yaml if present, else to prod if present, else to whichever exists
            if base_path:
                all_violations.append(
                    f"{base_path}: Missing required Datadog vars under 'env': {', '.join(missing_for_chart)}"
                )
            elif prod_path:
                all_violations.append(
                    f"{prod_path}: Missing required Datadog vars under 'env': {', '.join(missing_for_chart)}"
                )
            else:
                # Should not happen because we only create chart entries when at least one file exists
                pass

    if all_violations:
        print("❌ Datadog flags policy violations detected:")
        for v in all_violations:
            print(f"  - {v}")
        print(
            "\nIf you believe a flag must be enabled, please contact the DevOps team to request approval via the central allowlist."
        )
        sys.exit(1)

    # Also fail if required vars are missing entirely across all files? We already validate per-file.
    print("✅ Datadog flags policy passed for all checked values files.")


if __name__ == "__main__":
    main()


