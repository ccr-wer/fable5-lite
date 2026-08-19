#!/usr/bin/env python3
"""
base64-codec skill — USK v3 / stdin_stdout
입력: JSON {"text": "...", "operation": "encode"}
출력: JSON {"result": "..."}
"""
import sys
import json
import base64


def process(text: str, operation: str) -> dict:
    if operation == 'encode':
        result = base64.b64encode(text.encode('utf-8')).decode('ascii')
        return {"result": result, "operation": operation}

    elif operation == 'decode':
        try:
            # padding 자동 보정
            padded = text + '=' * (-len(text) % 4)
            result = base64.b64decode(padded).decode('utf-8')
            return {"result": result, "operation": operation}
        except Exception as e:
            return {"error": f"Decode failed: {e}"}

    elif operation == 'encode_urlsafe':
        result = base64.urlsafe_b64encode(text.encode('utf-8')).decode('ascii')
        return {"result": result, "operation": operation}

    elif operation == 'decode_urlsafe':
        try:
            padded = text + '=' * (-len(text) % 4)
            result = base64.urlsafe_b64decode(padded).decode('utf-8')
            return {"result": result, "operation": operation}
        except Exception as e:
            return {"error": f"URL-safe decode failed: {e}"}

    elif operation == 'validate':
        try:
            padded = text + '=' * (-len(text) % 4)
            base64.b64decode(padded, validate=True)
            return {"valid": True, "operation": operation}
        except Exception:
            return {"valid": False, "operation": operation}

    else:
        return {"error": f"Unknown operation: {operation}. Valid: encode, decode, encode_urlsafe, decode_urlsafe, validate"}


def main():
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({"error": "Empty input"}))
        sys.exit(1)
    try:
        inp = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON: {e}"}))
        sys.exit(1)

    text = inp.get("text")
    if text is None:
        print(json.dumps({"error": "Missing required field: text"}))
        sys.exit(1)
    if not isinstance(text, str):
        print(json.dumps({"error": "Field 'text' must be a string"}))
        sys.exit(1)

    operation = inp.get("operation")
    if not operation:
        print(json.dumps({"error": "Missing required field: operation"}))
        sys.exit(1)

    result = process(text, operation)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
