---
name: testing-for-xss-vulnerabilities
description: Specialized XSS testing using pre-built automation scripts for reflected and stored vectors.
---

# Testing for XSS Vulnerabilities

This skill provides automated scripts for discovering and validating Cross-Site Scripting (XSS) vulnerabilities.

## Available Scripts

### 1. `xss_scanner.py`
Automated scanner that tests a target URL for reflected XSS by injecting a variety of payloads into query parameters.
- **Usage**: `python3 xss_scanner.py --url <target_url>`
- **Features**: 
  - Context-aware payload selection
  - WAF detection and evasion
  - Result triage and reporting

### 2. `xss_payload_generator.py`
Generates optimized XSS payloads for specific contexts (HTML, Attribute, Script block).
- **Usage**: `python3 xss_payload_generator.py --context <context>`

## Instructions
1. Use `execute_skill_script` to run these specialized scripts.
2. Provide the target URL or parameters as arguments.
3. Review the output for confirmed reflections and execution markers.
