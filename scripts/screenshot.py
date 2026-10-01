"""Shoot every app screen for README/collage: login, dashboard, documents, facts, conflicts, normalizer, ask, ask-answer."""

from __future__ import annotations

import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:3000"
OUT = Path(__file__).resolve().parents[1] / "docs" / "images"
W, H = 1440, 900  # stable size for collage grid
PW = "sih-demo-2026-mrip"

SHOTS: list[tuple[str, str, str, str | None]] = [
    # (path, out_filename, label, action)
    ("/login", "00-login.png", "Sign in", None),
    ("/", "01-dashboard.png", "Dashboard", None),
    ("/documents", "02-documents.png", "Documents", None),
    ("/facts", "03-facts.png", "Facts", None),
    ("/conflicts", "04-conflicts.png", "Conflicts", None),
    ("/normalize", "05-normalizer.png", "Normalizer", None),
    ("/ask", "06-ask.png", "Ask (idle)", None),
    ("/ask", "07-ask-answered.png", "Ask (answered)", "ask"),
]


def login(page) -> None:
    page.goto(f"{BASE}/login", wait_until="domcontentloaded")
    page.wait_for_timeout(800)
    page.fill('input[name="username"]', "admin")
    page.fill('input[name="password"]', PW)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")
    time.sleep(0.6)
    if "/login" in page.url:
        page.goto(f"{BASE}/", wait_until="domcontentloaded")
        page.wait_for_timeout(800)


def settle(page) -> None:
    page.wait_for_timeout(1200)
    for sel in ["#next-route-announcer", "[data-nextjs-toast]"]:
        try:
            page.evaluate(f"document.querySelector('{sel}')?.remove()")
        except Exception:
            pass


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        ctx = browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=2)
        page = ctx.new_page()

        import urllib.request

        for name, url in [("api", "http://127.0.0.1:8000/api/health"), ("web", f"{BASE}/login")]:
            try:
                urllib.request.urlopen(url, timeout=5).read()
                print(f"  {name} ok")
            except Exception as e:
                print(f"  {name} unreachable: {e}")

        login(page)
        print(f"  after login: {page.url}")

        for path, filename, label, action in SHOTS:
            target = f"{BASE}{path}"
            if page.url.rstrip("/") != target.rstrip("/"):
                page.goto(target, wait_until="domcontentloaded")
                page.wait_for_load_state("networkidle")
            settle(page)

            if action == "ask":
                # Drive the Ask composer via the example chips (reliable React onChange)
                # then submit the form by Enter — avoids disabled Ask button race.
                try:
                    chip = page.locator('button:has-text("compare coal production across")').first
                    chip.click()
                    page.wait_for_timeout(300)
                    page.locator('input[aria-label="Question"]').first.press("Enter")
                    page.wait_for_timeout(900)
                    try:
                        page.wait_for_selector("text=Comparison", timeout=7000)
                    except Exception:
                        print(f"  ask: Comparison heading not yet visible (route: {page.url})")
                    page.wait_for_timeout(700)
                    settle(page)
                except Exception as e:
                    print(f"  ask action warn: {e}")

            out = OUT / filename
            page.screenshot(path=str(out), full_page=False)
            print(f"  {label:20s} -> {out.name}  ({out.stat().st_size/1024:.0f} KB)")

        browser.close()

    print("  done ->", OUT)
    for f in sorted(OUT.glob("0*.png")):
        print("   ", f.name, f"{f.stat().st_size/1024:.0f} KB")


if __name__ == "__main__":
    main()
