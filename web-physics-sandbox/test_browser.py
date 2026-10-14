"""
Browser test for the physics sandbox: loads every scene in headless Chromium, uses the tools with the mouse,
and checks that nothing explodes, nothing throws, and the simulation keeps up.

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


STATE = """() => { const w = window.__world(); let maxV = 0, nan = false, minY = 1e9;
  for (const b of w.bodies) { if (b.static) continue; const v = Math.hypot(b.vel.x, b.vel.y);
    if (!isFinite(v) || !isFinite(b.pos.x)) nan = true; maxV = Math.max(maxV, v); minY = Math.min(minY, b.pos.y); }
  return { n: w.bodies.length, joints: w.joints.length, maxV, nan, minY }; }"""

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(URL)
    page.wait_for_timeout(300)
    print("Physics sandbox in a real browser:")

    start = page.evaluate(STATE)
    check("the pyramid scene loads with 91 boxes", start["n"] == 92, start)
    page.wait_for_timeout(2500)
    s = page.evaluate(STATE)
    check("the pyramid is still standing after 2.5 s (nothing moving fast)", s["maxV"] < 0.5 and not s["nan"], s)

    for scene in ["Dominoes", "Bridge", "Wrecking ball", "Avalanche"]:
        page.click(f"#scenes button:text-is('{scene}')")
        page.wait_for_timeout(3500)
        s = page.evaluate(STATE)
        check(f"{scene}: runs 3.5 s with no NaNs or runaway speeds", not s["nan"] and s["maxV"] < 60 and s["minY"] > -1, s)
        if scene == "Wrecking ball":
            page.screenshot(path=os.path.join(HERE, "screenshot.png"))
        if scene == "Bridge":
            check("Bridge: the joints hold the planks up", s["joints"] == 23 and s["minY"] > -0.5, s)

    page.click("#scenes button:text-is('Pyramid')")
    page.wait_for_timeout(200)
    page.click("#tools button:text-is('Ball')")
    before = page.evaluate(STATE)["n"]
    page.mouse.click(300, 150)
    page.mouse.click(1000, 150)
    check("the Ball tool spawns a ball where you click", page.evaluate(STATE)["n"] == before + 2)

    page.click("#tools button:text-is('Drag')")
    top = page.evaluate("""() => { const w = window.__world(); const b = w.bodies.reduce((a, b) => b.pos.y > a.pos.y && !b.static && b.shape.type === 'poly' ? b : a);
      return { id: w.bodies.indexOf(b), y: b.pos.y }; }""")
    # find the top box on screen by asking the page to project it
    xy = page.evaluate(f"""() => {{ const w = window.__world(), b = w.bodies[{top['id']}], c = document.getElementById('c');
      const r = c.getBoundingClientRect(); const s = Math.min(c.width / 42, c.height / 22), cy = (c.height / s) / 2 - 1.5, dpr = c.width / r.width;
      return [(c.width / 2 + b.pos.x * s) / dpr, (c.height / 2 - (b.pos.y - cy) * s) / dpr]; }}""")
    page.mouse.move(*xy)
    page.mouse.down()
    page.mouse.move(xy[0] + 200, xy[1] - 150, steps=15)
    page.wait_for_timeout(800)
    lifted = page.evaluate(f"() => window.__world().bodies[{top['id']}].pos.y")
    page.mouse.up()
    check("dragging the top box lifts it with the mouse spring", lifted > top["y"] + 2, f"{top['y']:.2f} -> {lifted:.2f}")

    page.click("#tools button:text-is('Explode')")
    page.mouse.click(640, 600)
    page.wait_for_timeout(100)
    check("Explode pushes bodies away", page.evaluate(STATE)["maxV"] > 3)

    page.click("#pause")
    a = page.evaluate("() => window.__world().time")
    page.wait_for_timeout(400)
    check("Pause stops time", page.evaluate("() => window.__world().time") == a)
    page.click("#step")
    check("Step advances exactly one frame", abs(page.evaluate("() => window.__world().time") - a - 1 / 60) < 1e-9)

    page.click("#pause")
    page.click("#scenes button:text-is('Pyramid')")
    ms = page.evaluate("() => new Promise(r => { const w = window.__world(); const t = performance.now(); for (let i = 0; i < 60; i++) w.step(1/60); r((performance.now() - t) / 60); })")
    check(f"the 91-box pyramid steps in {ms:.1f} ms (budget 16 ms)", ms < 16)
    check("no JavaScript errors", not errors, errors)
    browser.close()

print(f"\n{passed} passed, {failed} failed")
print("ALL TESTS PASSED" if failed == 0 else "SOME TESTS FAILED")
sys.exit(1 if failed else 0)
