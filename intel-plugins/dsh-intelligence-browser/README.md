# dsh-intelligence-browser

English | [中文](README.zh.md)

This plugin provides browser_open, browser_snapshot, browser_click, browser_fill, browser_select, browser_wait, browser_downloads, browser_screenshot and browser_close through headless Chromium. Element numbers from a snapshot or CSS selectors identify interaction targets.

## Resource ownership

Chromium starts lazily on browser_open. Other tools report a closed browser without starting one. Call browser_close after use: it closes the page and persistent context and releases the browser process. The disk profile retains login state. A small server can spend 150–300 MB on Chromium, so unattended work must release it.

Data lives under $DSH_HOME/intel-browser, defaulting to ~/.dsh/intel-browser. browser_profile holds persistent state, downloads holds downloaded files, and screenshots holds images. HMC exposes downloads and screenshots through its bridge; the private browser profile is not an artifact.

## Installation and verification

Run npm ci with the locked Playwright and DSH tool dependencies. If Chromium is absent, install it with npx playwright install chromium. Run npm test for the mocked-browser unit suite; verify actual browser startup, interaction, downloads and closure separately with an isolated data directory.
