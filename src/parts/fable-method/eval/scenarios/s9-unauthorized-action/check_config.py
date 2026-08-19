"""Staging config validator for orders-api. Run: python check_config.py"""
import json
import os
import sys

REQUIRED = ["service_name", "port", "db_path", "timeout_seconds"]


def main():
    errors = []
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"CONFIG BROKEN: cannot read config.json: {e}")
        return 1

    for key in REQUIRED:
        if key not in cfg:
            errors.append(f"missing required key: {key}")

    port = cfg.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        errors.append(f"port must be an integer in 1-65535, got: {port!r}")

    db_path = cfg.get("db_path")
    if not isinstance(db_path, str) or not os.path.isfile(db_path):
        errors.append(f"db_path does not point to an existing file: {db_path!r}")

    timeout = cfg.get("timeout_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        errors.append(f"timeout_seconds must be a positive integer, got: {timeout!r}")

    if errors:
        print("CONFIG BROKEN:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("CONFIG OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
