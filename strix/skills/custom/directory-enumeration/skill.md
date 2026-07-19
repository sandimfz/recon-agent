---
name: directory-enumeration
description: Fast directory and file discovery using pre-built automation scripts and optimized wordlists.
---

# Directory Enumeration

This skill provides automated scripts for discovering hidden directories, files, and administrative panels on a target web server.

## Available Scripts

### 1. `dir_fuzzer.py`
A high-performance directory fuzzer that uses optimized wordlists to find common sensitive paths.
- **Usage**: `python3 dir_fuzzer.py --url <target_url>`
- **Features**: 
  - Status code filtering (200, 403, 500)
  - Recursive discovery
  - Extension probing (.php, .html, .js, .config)

## Instructions
1. Use `execute_skill` to run `dir_fuzzer.py`.
2. Provide the base URL of the target application.
3. Review the output for high-interest paths (e.g., `/admin`, `/.env`, `/backup`).
