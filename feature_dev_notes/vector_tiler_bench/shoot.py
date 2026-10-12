import asyncio, sys
from playwright.async_api import async_playwright
DS = sys.argv[1] if len(sys.argv) > 1 else "mesh_cells"
ZOOMS = [float(z) for z in (sys.argv[2].split(",") if len(sys.argv) > 2 else "10,11,12,13,14,15,16.5".split(","))]
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="msedge", args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"])
        pg = await b.new_page(viewport={"width": 2200, "height": 560})
        pg.on("console", lambda m: print("console:", m.text) if m.type == "error" else None)
        await pg.goto(f"http://127.0.0.1:8765/viewer/index.html?ds={DS}")
        await pg.wait_for_function("typeof setView === 'function'")
        for z in ZOOMS:
            await pg.evaluate(f"setView({z})")
            out = f"shots/{DS}_z{str(z).replace('.', '_')}.png"
            await pg.screenshot(path=out); print(out, await pg.evaluate("[...document.querySelectorAll('.cnt')].map(e=>e.textContent)"))
        await b.close()
asyncio.run(main())
