"""
Automated Browser Session Manager
==================================
A demonstration tool showcasing automated browser interaction capabilities
using SeleniumBase's CDP (Chrome DevTools Protocol) mode.

Features:
    - Geolocation-aware browser sessions
    - Automated consent/cookie banner handling
    - Multi-driver session management
    - Configurable target URLs and session parameters

Requirements:
    - seleniumbase
    - requests

Usage:
    python browser_session_manager.py [--url URL] [--proxy PROXY] [--sessions N]
"""

import argparse
import base64
import logging
import random
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import requests
from seleniumbase import SB

# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("BrowserSessionManager")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GEOLOCATION_API_URL = "http://ip-api.com/json/"
DEFAULT_SESSION_DURATION_RANGE = (450, 800)
DEFAULT_EXTRA_SESSIONS = 1
CONSENT_BUTTON_SELECTOR = 'button:contains("Accept")'
WATCH_BUTTON_SELECTOR = 'button:contains("Start Watching")'
STREAM_INFO_SELECTOR = "#live-channel-stream-information"


class Platform(Enum):
    """Supported streaming platforms."""
    TWITCH = "twitch"
    YOUTUBE = "youtube"


PLATFORM_URL_TEMPLATES: dict[Platform, str] = {
    Platform.TWITCH: "https://www.twitch.tv/{channel}",
    Platform.YOUTUBE: "https://www.youtube.com/@{channel}/live",
}


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GeoData:
    """Immutable geolocation data for browser session configuration."""
    latitude: float
    longitude: float
    timezone_id: str
    country_code: str

    @property
    def locale(self) -> str:
        """Derive a locale string from the country code (e.g. 'US' → 'en-US')."""
        code = self.country_code.upper()
        locale_map: dict[str, str] = {
            "US": "en-US", "GB": "en-GB", "FR": "fr-FR",
            "DE": "de-DE", "ES": "es-ES", "BR": "pt-BR",
            "JP": "ja-JP", "KR": "ko-KR", "IT": "it-IT",
        }
        return locale_map.get(code, f"en-{code}")


@dataclass
class SessionConfig:
    """Configuration for a browser automation session."""
    target_url: str
    geo: GeoData
    proxy: Optional[str] = None
    duration_range: tuple[int, int] = DEFAULT_SESSION_DURATION_RANGE
    extra_sessions: int = DEFAULT_EXTRA_SESSIONS
    ad_block: bool = True
    disable_webgl: bool = True

    def random_duration(self) -> int:
        """Return a random session duration within the configured range."""
        return random.randint(*self.duration_range)


# ---------------------------------------------------------------------------
# Geolocation Service
# ---------------------------------------------------------------------------
def fetch_geolocation(api_url: str = GEOLOCATION_API_URL) -> GeoData:
    """
    Fetch geolocation data from an external IP-based API.

    Returns:
        GeoData populated with latitude, longitude, timezone, and country code.

    Raises:
        ConnectionError: If the API is unreachable.
        KeyError: If the response payload is missing expected fields.
    """
    logger.info("Fetching geolocation data from %s", api_url)
    try:
        response = requests.get(api_url, timeout=10)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise ConnectionError(f"Failed to fetch geolocation data: {exc}") from exc

    required_keys = {"lat", "lon", "timezone", "countryCode"}
    missing = required_keys - data.keys()
    if missing:
        raise KeyError(f"Geolocation response missing fields: {missing}")

    geo = GeoData(
        latitude=data["lat"],
        longitude=data["lon"],
        timezone_id=data["timezone"],
        country_code=data["countryCode"].lower(),
    )
    logger.info(
        "Geolocation acquired — lat=%.4f, lon=%.4f, tz=%s",
        geo.latitude, geo.longitude, geo.timezone_id,
    )
    return geo


# ---------------------------------------------------------------------------
# URL Helpers
# ---------------------------------------------------------------------------
def decode_channel_name(encoded: str) -> str:
    """Decode a base64-encoded channel name."""
    return base64.b64decode(encoded).decode("utf-8")


def build_target_url(channel: str, platform: Platform = Platform.TWITCH) -> str:
    """Build the full URL for a given channel and platform."""
    template = PLATFORM_URL_TEMPLATES[platform]
    return template.format(channel=channel)


# ---------------------------------------------------------------------------
# Browser Interaction Helpers
# ---------------------------------------------------------------------------
def _dismiss_consent(driver, label: str = "primary") -> None:
    """Click the consent / cookie-accept button if it is present."""
    if driver.is_element_present(CONSENT_BUTTON_SELECTOR):
        logger.info("[%s] Dismissing consent banner.", label)
        driver.cdp.click(CONSENT_BUTTON_SELECTOR, timeout=4)


def _click_start_watching(driver, label: str = "primary") -> bool:
    """
    Click the 'Start Watching' button if present.

    Returns:
        True if the button was found and clicked, False otherwise.
    """
    if driver.is_element_present(WATCH_BUTTON_SELECTOR):
        logger.info("[%s] Clicking 'Start Watching'.", label)
        driver.cdp.click(WATCH_BUTTON_SELECTOR, timeout=4)
        return True
    return False


def _activate_session(driver, config: SessionConfig, label: str = "primary") -> None:
    """Activate a CDP session and handle initial consent / watch prompts."""
    logger.info("[%s] Activating CDP session → %s", label, config.target_url)
    driver.activate_cdp_mode(
        config.target_url,
        tzone=config.geo.timezone_id,
        geoloc=(config.geo.latitude, config.geo.longitude),
    )
    driver.sleep(2)
    _dismiss_consent(driver, label)
    driver.sleep(2)


def _wait_and_interact(driver, config: SessionConfig, label: str = "primary") -> None:
    """Wait for the stream page to load, then dismiss overlays."""
    driver.sleep(12)
    if _click_start_watching(driver, label):
        driver.sleep(10)
    _dismiss_consent(driver, label)


# ---------------------------------------------------------------------------
# Core Session Runner
# ---------------------------------------------------------------------------
def _spawn_extra_driver(
    parent_driver,
    config: SessionConfig,
    index: int,
) -> object:
    """Spawn an additional undetectable browser driver and initialise it."""
    label = f"extra-{index}"
    logger.info("[%s] Spawning extra driver.", label)

    extra = parent_driver.get_new_driver(undetectable=True)
    _activate_session(extra, config, label)
    _wait_and_interact(extra, config, label)
    return extra


def run_session(config: SessionConfig) -> bool:
    """
    Execute a single browser session lifecycle.

    Returns:
        True if the stream was live and the session completed normally,
        False if the stream was offline (signals the caller to stop).
    """
    proxy_arg = config.proxy if config.proxy else False
    chromium_args = "--disable-webgl" if config.disable_webgl else ""

    with SB(
        uc=True,
        locale="en",
        browser="brave",
        incognito=True,
        xvfb=True,
        ad_block=config.ad_block,
        chromium_arg=chromium_args,
        proxy=proxy_arg,
    ) as driver:
        _activate_session(driver, config)
        _wait_and_interact(driver, config)

        # --- Stream liveness check ----------------------------------------
        if not driver.is_element_present(STREAM_INFO_SELECTOR):
            logger.warning("Stream is offline. Ending session loop.")
            return False

        logger.info("Stream is LIVE — opening extra sessions.")
        _dismiss_consent(driver)

        extra_drivers: list = []
        for i in range(1, config.extra_sessions + 1):
            extra = _spawn_extra_driver(driver, config, index=i)
            extra_drivers.append(extra)

        duration = config.random_duration()
        logger.info("Watching for %d seconds.", duration)
        driver.sleep(duration)
    driver.save_screenshot("komu.png")
    logger.info("Session completed successfully.")
    return True


def run_loop(config: SessionConfig) -> None:
    """Continuously run sessions until the stream goes offline."""
    logger.info("Starting session loop — target: %s", config.target_url)
    session_number = 0

    while True:
        session_number += 1
        logger.info("=== Session #%d ===", session_number)
        try:
            stream_live = run_session(config)
            if not stream_live:
                logger.info("Stream offline — exiting loop.")
                break
        except Exception:
            logger.exception("Session #%d encountered an error.", session_number)
            cooldown = random.randint(10, 30)
            logger.info("Cooling down for %d seconds before retry.", cooldown)
            time.sleep(cooldown)


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Automated Browser Session Manager (demo tool)",
    )
    parser.add_argument(
        "--channel",
        type=str,
        default=None,
        help="Channel name (plain text). Overrides --encoded-channel.",
    )
    parser.add_argument(
        "--encoded-channel",
        type=str,
        default="YnJ1dGFsbGVz",
        help="Base64-encoded channel name (default: demo value).",
    )
    parser.add_argument(
        "--platform",
        type=str,
        choices=[p.value for p in Platform],
        default=Platform.TWITCH.value,
        help="Streaming platform (default: twitch).",
    )
    parser.add_argument(
        "--proxy",
        type=str,
        default=None,
        help="Proxy address (e.g. socks5://127.0.0.1:9050).",
    )
    parser.add_argument(
        "--extra-sessions",
        type=int,
        default=DEFAULT_EXTRA_SESSIONS,
        help="Number of extra browser sessions to spawn (default: 1).",
    )
    parser.add_argument(
        "--min-duration",
        type=int,
        default=DEFAULT_SESSION_DURATION_RANGE[0],
        help="Minimum session duration in seconds.",
    )
    parser.add_argument(
        "--max-duration",
        type=int,
        default=DEFAULT_SESSION_DURATION_RANGE[1],
        help="Maximum session duration in seconds.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    """Application entry point."""
    args = parse_args(argv)

    # --- Resolve channel name ---------------------------------------------
    if args.channel:
        channel = args.channel
    else:
        channel = decode_channel_name(args.encoded_channel)
    logger.info("Channel: %s", channel)

    platform = Platform(args.platform)
    target_url = build_target_url(channel, platform)

    # --- Geolocation ------------------------------------------------------
    geo = fetch_geolocation()

    # --- Build config & run -----------------------------------------------
    config = SessionConfig(
        target_url=target_url,
        geo=geo,
        proxy=args.proxy,
        duration_range=(args.min_duration, args.max_duration),
        extra_sessions=args.extra_sessions,
    )
    run_loop(config)


if __name__ == "__main__":
    main()
