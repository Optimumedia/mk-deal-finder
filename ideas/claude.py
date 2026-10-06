"""The two Claude calls of a daily run: web research, then ideas as JSON.

Needs ANTHROPIC_API_KEY (setup_ideas.ps1 saves it to .anthropic, git-ignored).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import anthropic

from . import prompts

log = logging.getLogger(__name__)
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_CONTINUATIONS = 5


class ClaudeError(RuntimeError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write: int = 0
    cache_read: int = 0
    searches: int = 0
    sources: list[str] = field(default_factory=list)

    def add(self, u) -> None:
        self.input_tokens += u.input_tokens or 0
        self.output_tokens += u.output_tokens or 0
        self.cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0
        self.cache_read += getattr(u, "cache_read_input_tokens", 0) or 0
        stu = getattr(u, "server_tool_use", None)
        self.searches += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0

    def cost_usd(self, prices: dict) -> float:
        return (self.input_tokens * prices["input_per_mtok"] + self.output_tokens * prices["output_per_mtok"]
                + self.cache_write * prices["cache_write_per_mtok"] + self.cache_read * prices["cache_read_per_mtok"]
                ) / 1e6 + self.searches * prices["web_search_per_1000"] / 1000


class Claude:
    def __init__(self, run_cfg: dict, client: anthropic.Anthropic | None = None):
        self.cfg = run_cfg
        self.client = client or anthropic.Anthropic(max_retries=4, timeout=900)
        self.fallback = bool(run_cfg.get("refusal_fallback", True))
        self.usage = Usage()

    def _stream(self, **kwargs):
        if self.fallback:
            kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")
        try:
            with self.client.beta.messages.stream(**kwargs) as s:
                return s.get_final_message()
        except anthropic.BadRequestError as e:
            if self.fallback and "fallback" in str(e).lower():
                log.warning("refusal fallback not accepted (%s); retrying without it", e)
                self.fallback = False
                kwargs.pop("betas", None)
                kwargs.pop("fallbacks", None)
                with self.client.beta.messages.stream(**kwargs) as s:
                    return s.get_final_message()
            raise

    def _run(self, *, system: str, user: str, effort: str, tools=None, output_format=None,
             max_tokens: int = 64000) -> list:
        """One task, resuming server-tool pauses; returns every assistant content block in order."""
        messages = [{"role": "user", "content": user}]
        blocks = []
        output_config = {"effort": effort}
        if output_format:
            output_config["format"] = output_format
        for _ in range(MAX_CONTINUATIONS + 1):
            kwargs = dict(model=self.cfg["model"], max_tokens=max_tokens, system=system, messages=messages,
                          thinking={"type": "adaptive"}, output_config=output_config)
            if tools:
                kwargs["tools"] = tools
            msg = self._stream(**kwargs)
            self.usage.add(msg.usage)
            blocks += list(msg.content)
            if msg.stop_reason == "refusal":
                details = getattr(msg, "stop_details", None)
                raise ClaudeError(f"Claude declined the request ({getattr(details, 'category', None)})")
            if msg.stop_reason == "max_tokens":
                raise ClaudeError("the answer was cut off at max_tokens")
            if msg.stop_reason != "pause_turn":
                return blocks
            # A long server-tool turn paused: send it back as is and the server carries on.
            messages = messages + [{"role": "assistant", "content": msg.content}]
        raise ClaudeError(f"still paused after {MAX_CONTINUATIONS} continuations")

    def research(self, date: str, profile: str, lenses: dict[str, str]) -> tuple[str, list[str]]:
        """Web research for today's lenses → (Markdown brief, source URLs)."""
        lens_text = "\n".join(f"- {name}: {desc}" for name, desc in lenses.items())
        blocks = self._run(
            system=prompts.RESEARCH_SYSTEM,
            user=prompts.RESEARCH_TASK.format(date=date, profile=profile, lenses=lens_text),
            effort=self.cfg.get("research_effort", "medium"),
            tools=[{"type": "web_search_20260209", "name": "web_search",
                    "max_uses": int(self.cfg.get("max_web_searches", 10))}])
        sources: list[str] = []
        for b in blocks:
            if b.type == "web_search_tool_result" and isinstance(b.content, list):   # an error is an object
                sources += [r.url for r in b.content if getattr(r, "url", None)]
        text = "\n".join(b.text for b in blocks if b.type == "text").strip()
        if not text:
            raise ClaudeError("the research step returned no text")
        self.usage.sources = list(dict.fromkeys(sources))
        return text, self.usage.sources

    def ideate(self, *, date: str, profile: str, taste: str, research: str, lens_names: list[str],
               avoid: list[str], n: int, filters: dict, max_per_category: int) -> list[dict]:
        """Ideas from today's research, shaped by your taste → list of idea dicts (schema in prompts.py)."""
        system = prompts.IDEATE_SYSTEM.format(max_cost=filters["max_startup_cost_eur"],
                                              min_profit=filters["min_annual_profit_eur"],
                                              min_auto=filters["min_automation_pct"])
        user = prompts.IDEATE_TASK.format(
            date=date, profile=profile, taste=taste, research=research, lens_names=", ".join(lens_names),
            avoid="\n".join(f"- {t}" for t in avoid) or "(none yet)", n=n, max_per_category=max_per_category)
        blocks = self._run(system=system, user=user, effort=self.cfg.get("ideate_effort", "high"),
                           output_format={"type": "json_schema", "schema": prompts.idea_schema(lens_names)})
        text = next((b.text for b in blocks if b.type == "text"), "")
        try:
            return json.loads(text)["ideas"]
        except (ValueError, KeyError, TypeError) as e:
            raise ClaudeError(f"the ideas were not valid JSON: {e}") from e
