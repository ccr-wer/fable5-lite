---
spec: usk/1.0
name: base64-codec
version: 1.0.0
description: "텍스트와 Base64 인코딩 간 변환을 수행합니다. 일반 Base64, URL-safe Base64를 지원하며 UTF-8 문자열을 안전하게 처리합니다."
category: Encoding
tags: [base64, encode, decode, encoding, utility]
platform_compatibility: [OpenClaw, ClaudeCode, CustomAgent, any]

interface:
  type: cli
  entry_point: main.py
  runtime: python3
  call_pattern: stdin_stdout

input_schema:
  type: object
  required: [text, operation]
  properties:
    text:
      type: string
      description: "인코딩할 원본 텍스트 또는 디코딩할 Base64 문자열"
    operation:
      type: string
      description: "수행할 작업"
      enum: [encode, decode, encode_urlsafe, decode_urlsafe, validate]

output_schema:
  type: object
  properties:
    result:
      type: string
      description: "변환 결과"
    valid:
      type: boolean
      description: "유효한 Base64인지 여부 (validate 작업)"
    error:
      type: string
      description: "오류 메시지"

capabilities:
  - encoding
  - decoding
  - base64
  - text_transform

permissions:
  network: false
  filesystem: false
  subprocess: false
  env_vars: []

requirements:
  python: ">=3.8"
  packages: []

changelog:
  - version: 1.0.0
    date: "2026-04-09"
    changes: "초기 릴리스"
---

# base64-codec

Base64 인코딩/디코딩 유틸리티입니다.

## 예시

```json
{"text": "Hello, World!", "operation": "encode"}
```
→ `{"result": "SGVsbG8sIFdvcmxkIQ=="}`

```json
{"text": "SGVsbG8sIFdvcmxkIQ==", "operation": "decode"}
```
→ `{"result": "Hello, World!"}`
