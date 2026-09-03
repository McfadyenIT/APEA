"""DEPRECATED — removed in the recording-first redesign.

LT Metrics no longer crawls or drives a browser to discover the flow. The uploaded
BlazeMeter / JMeter / HAR recording is the single source of truth (it carries the
target URL and the full journey). This module is intentionally left empty so any
stale import fails loudly rather than silently pulling in Playwright.
"""
