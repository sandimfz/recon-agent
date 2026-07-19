import argparse
import requests
import sys

def fuzz_directories(base_url, wordlist):
    print(f"[*] Starting directory fuzzing on: {base_url}")
    results = []
    
    for word in wordlist:
        url = f"{base_url.rstrip('/')}/{word}"
        try:
            response = requests.get(url, timeout=5, allow_redirects=False)
            status = response.status_code
            
            if status in [200, 204, 301, 302, 307, 401, 403]:
                print(f"[+] Found: {url} (Status: {status})")
                results.append({"url": url, "status": status})
        except Exception as e:
            pass
            
    return results

def main():
    parser = argparse.ArgumentParser(description="Simple Directory Fuzzer")
    parser.add_argument("--url", required=True, help="Base URL to scan")
    args = parser.parse_args()

    # Small optimized wordlist
    wordlist = [
        "admin", "login", "api", "v1", "v2", "config", "setup",
        ".env", ".git", ".htaccess", "backup", "db", "uploads",
        "scripts", "css", "js", "images", "assets", "robots.txt",
        "sitemap.xml", "test", "dev", "old", "new", "portal"
    ]

    results = fuzz_directories(args.url, wordlist)
    
    if results:
        print("\n[SUMMARY] Discovered paths:")
        for r in results:
            print(f"- {r['url']} ({r['status']})")
    else:
        print("\n[!] No interesting paths found with basic wordlist.")

if __name__ == "__main__":
    main()
