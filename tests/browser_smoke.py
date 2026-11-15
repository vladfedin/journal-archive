"""Optional real-browser check: pip install playwright; playwright install chromium.

Run with a local server active: python tests/browser_smoke.py
"""
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1200}, device_scale_factor=1)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://127.0.0.1:8787/")
        expect(page.locator("#username")).to_be_visible()
        expect(page.locator("#password")).to_have_attribute("type", "password")
        expect(page.locator("#entry-limit")).to_have_value("10")
        expect(page.locator("#max-requests")).to_have_value("50")
        expect(page.locator("#images")).not_to_be_checked()
        page.screenshot(path="/tmp/ljarchive-desktop.png", full_page=True)
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        page.get_by_role("button", name="Посмотреть пример архива").click()
        expect(page.locator("#job-phase")).to_have_text("Пример", timeout=15000)
        page.locator("#open-result").click()
        expect(page.locator(".entry-card")).to_have_count(6)
        expect(page.locator(".entry-card .reaction")).to_have_count(6)
        page.locator("#year").select_option("2012")
        assert page.locator(".entry-card").count() == 1
        page.locator("#reset-filters").click()
        page.locator("#tag").select_option("книги")
        assert page.locator(".entry-card").count() == 1
        page.locator("#reset-filters").click()
        page.locator("#search").fill("полуночи")
        expect(page.locator(".entry-card")).to_have_count(1)
        assert "Совпадение в комментарии" in page.locator(".entry-card").inner_text()
        page.locator(".entry-card h3 a").click()
        page.wait_for_url("**/entries/3.html#comment-4")
        expect(page.locator('.post > .reactions [aria-label="Нравится: 5"]')).to_be_visible()
        expect(page.locator('#comment-4 [aria-label="Пиши ещё!: 1"]')).to_be_visible()
        page.locator('#comment-4 [aria-label="Пиши ещё!: 1"]').click()
        expect(page.locator('#comment-4 .reaction-detail[open] .reaction-people')).to_contain_text('sea_notes')
        page.locator('#comment-4 [aria-label="Пиши ещё!: 1"]').click()
        page.locator("#comment-4 .thread-toggle").click()
        assert page.locator("#comment-5").is_hidden()
        assert page.locator("#comment-6").is_hidden()
        assert page.locator("#comment-7").is_visible()
        page.get_by_role("button", name="Развернуть все").click()
        assert page.locator("#comment-5").is_visible()
        page.screenshot(path="/tmp/ljarchive-entry.png", full_page=True)
        page.goto(Path("archives/_demo/index.html").resolve().as_uri())
        page.locator("#search").fill("полуночи")
        expect(page.locator(".entry-card")).to_have_count(1)
        page.locator(".entry-card h3 a").click()
        assert page.locator("#comment-4").is_visible()
        page.goto("http://127.0.0.1:8787/")
        with page.expect_download() as downloaded:
            page.locator('a[href="/download/_demo.json"]').click()
        assert downloaded.value.suggested_filename == '_demo.json'
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path="/tmp/ljarchive-mobile.png", full_page=True)
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        # The archive itself is what gets read on a phone: filters fold away, threads stay inside the screen.
        page.goto("http://127.0.0.1:8787/archive/_demo/index.html")
        expect(page.locator("#year")).to_be_hidden()
        page.locator("#toggle-filters").click()
        expect(page.locator("#year")).to_be_visible()
        page.locator("#search").fill("полуночи")
        expect(page.locator(".entry-card mark")).to_have_text("полуночи")
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        page.goto("http://127.0.0.1:8787/archive/_demo/entries/3.html")
        page.locator('#comment-4 [aria-label="Пиши ещё!: 1"]').click()
        expect(page.locator("#comment-4 .reaction-detail[open] .reaction-people")).to_be_visible()
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        page.screenshot(path="/tmp/ljarchive-entry-mobile.png", full_page=True)
        # The header button overrides the system colour scheme and the choice survives a reload.
        assert page.evaluate("document.documentElement.dataset.theme") is None
        page.locator("#theme-toggle").click()
        assert page.evaluate("document.documentElement.dataset.theme") == "dark"
        page.reload()
        assert page.evaluate("document.documentElement.dataset.theme") == "dark"
        expect(page.locator("#theme-toggle")).to_have_attribute("aria-label", "Включить светлую тему")
        page.screenshot(path="/tmp/ljarchive-entry-mobile-dark.png")
        page.locator("#theme-toggle").click()
        assert page.evaluate("document.documentElement.dataset.theme") == "light"
        assert not errors, errors
        print("PASS: desktop, mobile, demo, filters, comment search, threaded replies, file://, no JS errors")
        browser.close()


if __name__ == "__main__":
    main()
