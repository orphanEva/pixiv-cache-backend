import asyncio
from playwright.async_api import async_playwright


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            print(browser.version)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
