"""Fake services used by the test-suite, the dev compose profile and the CI.

They are deterministic stand-ins: never use them as evidence that real hardware works.
"""

from __future__ import annotations

from fastapi import FastAPI


def create_fake(name: str) -> FastAPI:
    if name == "llm":
        from homelab_agent.fakes.llm import create_app as llm

        return llm()
    if name == "ha":
        from homelab_agent.fakes.ha import create_app as ha

        return ha()
    if name == "telegram":
        from homelab_agent.fakes.telegram import create_app as tg

        return tg()
    raise ValueError(name)
