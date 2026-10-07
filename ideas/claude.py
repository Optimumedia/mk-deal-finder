"""The two Claude calls of a daily run, through Claude Code on this PC: web research, then ideas as JSON.

Runs `claude -p` (Claude Code's non-interactive mode) logged in with your Claude
subscription, so a run costs nothing extra: it uses part of your plan's usage
limits. API keys are removed from its environment so it can never bill one.
setup_ideas.ps1 installs and logs in Claude Code and saves its path to .ideas.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import prompts

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = ROOT / "data" / "claude-work"        # empty folder: no project files or CLAUDE.md in the way
# Variables that would make Claude Code bill an API account instead of using the subscription.
BILLING_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
                "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
NATIVE_INSTALL = "irm https://claude.ai/install.ps1 | iex"


class ClaudeError(RuntimeError):
    pass


@dataclass
class Usage:
    calls: int = 0
    list_value_usd: float = 0.0     # what the same tokens would cost on the API; not charged on a subscription

    def add(self, result: dict) -> None:
        self.calls += 1
        self.list_value_usd += float(result.get("total_cost_usd") or 0)


def find_claude(configured: str | None = None) -> str:
    """Path to the Claude Code executable: .ideas setting, then PATH, then the native installer's folder."""
    candidates = [configured, shutil.which("claude"),
                  str(Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude"))]
    for c in candidates:
        if c and Path(c).is_file():
            if Path(c).suffix.lower() in (".cmd", ".bat", ".ps1"):
                raise ClaudeError(f"Claude Code at {c} is the npm wrapper, which can't pass the JSON schema safely. "
                                  f"Install the native build in PowerShell: {NATIVE_INSTALL}")
            return c
    raise ClaudeError(f"Claude Code isn't installed. In PowerShell run: {NATIVE_INSTALL} , then setup_ideas.ps1")


def subscription_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in BILLING_VARS}


class Claude:
    def __init__(self, run_cfg: dict, runner=subprocess.run, exe: str | None = None):
        self.cfg = run_cfg
        self.runner = runner
        self.exe = exe or find_claude(os.environ.get("IDEAS_CLAUDE_PATH"))
        self.usage = Usage()

    def _run(self, *, system: str, user: str, effort: str, tools: list[str], schema: dict | None = None) -> dict:
        args = [self.exe, "-p", "--output-format", "json", "--no-session-persistence", "--strict-mcp-config",
                "--permission-prompts", "none", "--model", self.cfg["model"], "--effort", effort,
                "--system-prompt", system, "--tools", ",".join(tools)]
        if tools:
            args += ["--allowedTools", ",".join(tools)]
        if self.cfg.get("fallback_model"):
            args += ["--fallback-model", self.cfg["fallback_model"]]
        if schema:
            args += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        try:
            proc = self.runner(args, input=user.encode("utf-8"), capture_output=True, cwd=WORK_DIR,
                               env=subscription_env(), timeout=int(self.cfg.get("timeout_minutes", 25)) * 60)
        except subprocess.TimeoutExpired as e:
            raise ClaudeError(f"Claude Code took longer than {self.cfg.get('timeout_minutes', 25)} minutes") from e
        except OSError as e:
            raise ClaudeError(f"couldn't start Claude Code ({self.exe}): {e}") from e
        out = (proc.stdout or b"").decode("utf-8", "replace").strip()
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        result = None
        for chunk in (out, out.splitlines()[-1] if out else ""):     # one JSON object, possibly after log lines
            try:
                result = json.loads(chunk)
                break
            except ValueError:
                continue
        if not isinstance(result, dict):
            hint = " Update it with: claude update" if re.search(r"unknown option|unknown argument", err, re.I) else ""
            raise ClaudeError(f"Claude Code failed (exit {proc.returncode}): {(err or out)[-400:]}{hint}")
        self.usage.add(result)
        if result.get("is_error") or result.get("subtype") != "success":
            # e.g. not logged in, usage limit reached, or the turn errored
            raise ClaudeError(f"Claude Code: {result.get('subtype')}: {str(result.get('result') or '')[:400]}")
        return result

    def research(self, date: str, profile: str, lenses: dict[str, str]) -> tuple[str, list[str]]:
        """Web research for today's lenses → (Markdown brief, source URLs)."""
        lens_text = "\n".join(f"- {name}: {desc}" for name, desc in lenses.items())
        result = self._run(
            system=prompts.RESEARCH_SYSTEM,
            user=prompts.RESEARCH_TASK.format(date=date, profile=profile, lenses=lens_text,
                                              max_searches=int(self.cfg.get("max_web_searches", 10))),
            effort=self.cfg.get("research_effort", "medium"), tools=["WebSearch", "WebFetch"])
        text = str(result.get("result") or "").strip()
        if not text:
            raise ClaudeError("the research step returned no text")
        urls = re.findall(r"https?://[^\s)\]>\"'<]+", text)
        return text, list(dict.fromkeys(u.rstrip(".,;:") for u in urls))

    def ideate(self, *, date: str, profile: str, taste: str, research: str, lens_names: list[str],
               avoid: list[str], n: int, filters: dict, max_per_category: int) -> list[dict]:
        """Ideas from today's research, shaped by your taste → list of idea dicts (schema in prompts.py)."""
        system = prompts.IDEATE_SYSTEM.format(max_cost=filters["max_startup_cost_eur"],
                                              min_profit=filters["min_annual_profit_eur"],
                                              min_auto=filters["min_automation_pct"])
        user = prompts.IDEATE_TASK.format(
            date=date, profile=profile, taste=taste, research=research, lens_names=", ".join(lens_names),
            avoid="\n".join(f"- {t}" for t in avoid) or "(none yet)", n=n, max_per_category=max_per_category)
        result = self._run(system=system, user=user, effort=self.cfg.get("ideate_effort", "high"), tools=[],
                           schema=prompts.idea_schema(lens_names))
        data = result.get("structured_output")
        if data is None:
            try:
                data = json.loads(result.get("result") or "")
            except ValueError as e:
                raise ClaudeError(f"the ideas were not valid JSON: {e}") from e
        if not isinstance(data, dict) or not isinstance(data.get("ideas"), list):
            raise ClaudeError("the ideas came back in an unexpected shape")
        return data["ideas"]
