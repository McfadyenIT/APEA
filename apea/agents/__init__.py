"""APEA specialized sub-agents.

Each module is a focused agent that the orchestrator (server) routes work to:
    discovery  — Discovery & Context Agent (crawl + domain detection)
    planner    — Test Planning Agent (workload modeling)
    generator  — Locust Script Generator Agent
    reviewer   — Script Review Agent (senior perf engineer audit)
    executor   — Execution & Monitoring Agent
    analyzer   — Post-Run Intelligence & RCA Agent
"""
