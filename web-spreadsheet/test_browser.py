"""
Browser test for FranzSheets: drives the real page with the keyboard and mouse in headless Chromium.

Run:  pip install playwright && python -m playwright install chromium && python test_browser.py
"""
import os
import sys

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "file://" + os.path.join(HERE, "index.html").replace("\\", "/")
passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def cell(page, key):
    col, row = ord(key[0]) - 65, int(key[1:]) - 1
    return page.locator(f'td[data-r="{row}"][data-c="{col}"]')


def type_into(page, key, text):
    cell(page, key).click()
    page.keyboard.type(text)
    page.keyboard.press("Enter")


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 760})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(URL)
    page.evaluate("localStorage.clear()")
    page.reload()
    print("FranzSheets in a real browser:")

    type_into(page, "A1", "12")
    type_into(page, "A2", "30")
    type_into(page, "A3", "=SUM(A1:A2)*2")
    check("typing a formula shows its value", cell(page, "A3").inner_text() == "84")
    type_into(page, "A1", "8")
    check("editing a cell recalculates the formulas that use it", cell(page, "A3").inner_text() == "76")

    cell(page, "A3").click()
    check("the formula bar shows the formula, not the value", page.input_value("#formula") == "=SUM(A1:A2)*2")
    page.keyboard.press("Control+c")
    cell(page, "B3").click()
    page.keyboard.press("Control+v")
    check("copy/paste shifts relative references (A -> B)", page.evaluate("window.__sheet().raw('B3')") == "=SUM(B1:B2)*2")

    type_into(page, "C1", "=C2+1")
    type_into(page, "C2", "=C1")
    check("a circular reference shows #CYCLE!", cell(page, "C1").inner_text() == "#CYCLE!")
    type_into(page, "D1", "=1/0")
    check("errors are shown in the cell", cell(page, "D1").inner_text() == "#DIV/0!")

    cell(page, "A1").click()
    page.keyboard.press("Shift+ArrowDown")
    page.keyboard.press("Shift+ArrowDown")
    stats = page.inner_text("#stats")
    check("selecting a range shows Sum / Average in the status bar", "Sum 114" in stats and "Average 38" in stats, stats)

    page.click("#sample")
    check("sample grade sheet loads with formulas", cell(page, "E4").inner_text() == "97.5" and cell(page, "F4").inner_text() == "A")
    type_into(page, "D4", "60")
    check("changing Carlo's exam updates average, letter grade and class stats",
          cell(page, "E4").inner_text() == "78" and cell(page, "F4").inner_text() == "C"
          and cell(page, "E14").inner_text() == "1" and cell(page, "E13").inner_text() == "8 of 8", f"E4={cell(page, 'E4').inner_text()} F4={cell(page, 'F4').inner_text()}")
    cell(page, "E2").click()
    page.keyboard.press("Shift+ArrowDown")
    for _ in range(6):
        page.keyboard.press("Shift+ArrowDown")
    page.click("#chartBtn")
    check("chart panel opens for the selection", page.is_visible("#chart") and "8 values" in page.inner_text("#chartTitle"))
    page.screenshot(path=os.path.join(HERE, "screenshot.png"))

    page.reload()
    check("the sheet is saved in the browser and survives a reload", cell(page, "E4").inner_text() == "78")
    check("no JavaScript errors", not errors, errors)
    browser.close()

print(f"\n{passed} passed, {failed} failed")
print("ALL TESTS PASSED" if failed == 0 else "SOME TESTS FAILED")
sys.exit(1 if failed else 0)
