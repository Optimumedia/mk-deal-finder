"""Prompts and the JSON schema each idea must follow."""
from __future__ import annotations

CATEGORIES = ["Productized service", "Micro-SaaS", "Automation for small businesses", "AI-run agency",
              "Content and media", "Education and info products", "E-commerce", "Marketplace or directory",
              "Data and lead generation", "Developer tool or API", "Local services", "Arbitrage"]
BUSINESS_MODELS = ["Subscription", "Retainer", "One-off sales", "Usage-based", "Commission or affiliate",
                   "Advertising or sponsorship", "Licensing"]
CHANNELS = ["SEO and content", "Cold email and outbound", "Paid ads", "Marketplaces and app stores",
            "Social and creators", "Partnerships and resellers", "Communities and forums", "Local direct sales",
            "Product-led or viral"]
CUSTOMER_TYPES = ["Small businesses", "Mid-market and enterprise", "Consumers", "Creators and freelancers",
                  "Public sector and NGOs"]
GEOGRAPHIES = ["North Macedonia", "Balkans", "EU", "US and UK", "Global online"]

RESEARCH_SYSTEM = """You are the research desk of a one-person venture studio. Each morning you search the web for \
fresh, concrete signals that open small-business opportunities: a founder should be able to start with little \
money and have Claude (an AI model, via API and agents) do most of the recurring work.

Prefer primary and specific sources: regulator pages, launch announcements, pricing pages, marketplace data, \
forum threads where people describe a real pain. Generic "AI business ideas" listicles are not signals."""

RESEARCH_TASK = """Today is {date}.

Founder:
{profile}

Today's research lenses:
{lenses}

Search the web for signals from roughly the last 60 days that fit these lenses, using at most {max_searches} \
searches. Return 8 to 12 signals as a Markdown list. For each signal give:
- **Signal**: what happened or what people are asking for, specific, with numbers where you have them
- **Opening**: who now has a problem or a budget, and what they pay for it today
- **Source**: the URL and its date

Leave out anything you could not back with a source. Signals only, no business ideas yet."""

IDEATE_SYSTEM = """You turn market signals into business ideas for one specific founder. Every idea must:
- start for at most €{max_cost} in total (tools, domains, test ads, freelancers) and earn its first revenue \
within about 3 months;
- have a credible path to at least €{min_profit:,} profit a year, with the arithmetic shown \
(price × customers − costs) using conservative numbers;
- let Claude do most of the recurring work (research, writing, code, support, outreach drafts, reporting): \
name the steps Claude runs and the steps that stay human, and put the share in automation_pct \
(at least {min_auto}%);
- be legal and honest: no licences the founder lacks, no spam, no scraping sites whose terms forbid it, no \
deceptive practices;
- be specific: a named customer, a named offer, a price. "Start an AI agency" is not an idea.

Be calibrated. profit_low_eur is what a competent founder plausibly makes in year 2 if it works; \
profit_high_eur is a realistic ceiling, not a fantasy. self_score is your honest 1-10 rating of the \
opportunity for this founder; use the whole scale.

Each idea comes from the signals you are given: put the signal and its URL in evidence."""

IDEATE_TASK = """Today is {date}.

Founder:
{profile}

What the founder's ratings say about their taste:
{taste}

Today's research (lenses: {lens_names}):
{research}

Earlier ideas. Don't repeat these or reword them:
{avoid}

Write {n} distinct ideas. At most {max_per_category} may share a category. Put each idea's research lens in \
the lens field, exactly as named above. Make 2 of the {n} deliberately unlike what the founder has rated \
highly so far, so their taste keeps being tested."""


def _str(desc: str) -> dict:
    return {"type": "string", "description": desc}


def _int(desc: str) -> dict:
    return {"type": "integer", "description": desc}


def _list(desc: str) -> dict:
    return {"type": "array", "items": {"type": "string"}, "description": desc}


def _enum(values: list[str], desc: str) -> dict:
    return {"type": "string", "enum": values, "description": desc}


def idea_schema(lens_names: list[str]) -> dict:
    props = {
        "title": _str("Short name, max 8 words"),
        "one_liner": _str("One sentence: who pays whom for what"),
        "lens": _enum(lens_names, "The research lens this idea came from"),
        "problem": _str("The pain, in the customer's words"),
        "customer": _str("Who exactly buys it: role, company size, where they are"),
        "offer": _str("What is sold, and at what price"),
        "claude_does": _list("Recurring steps Claude runs, 2-5 short items"),
        "founder_does": _list("Steps that stay human, 1-3 short items"),
        "startup_cost_eur": _int("Total money needed before the first sale"),
        "monthly_cost_eur": _int("Running cost per month at the start, including API usage"),
        "weeks_to_revenue": _int("Weeks from starting to the first paying customer"),
        "profit_low_eur": _int("Conservative annual profit in year 2"),
        "profit_high_eur": _int("Realistic annual profit ceiling"),
        "profit_math": _str("The arithmetic behind the profit numbers, one or two lines"),
        "automation_pct": _int("Share of the recurring work Claude can do, 0-100"),
        "validation_test": _str("How to test demand within a week for under €100"),
        "first_week": _list("First three concrete actions"),
        "risks": _list("Main risks, 1-3 short items"),
        "moat": _str("Why it is hard to copy once running, or 'none' if it isn't"),
        "evidence": {"type": "array", "description": "Signals from today's research behind this idea",
                     "items": {"type": "object", "additionalProperties": False, "required": ["signal", "url"],
                               "properties": {"signal": _str("The signal in one line"),
                                              "url": _str("Its source URL")}}},
        "category": _enum(CATEGORIES, "Kind of business"),
        "business_model": _enum(BUSINESS_MODELS, "How it earns"),
        "channel": _enum(CHANNELS, "Main way to reach the first customers"),
        "customer_type": _enum(CUSTOMER_TYPES, "Main customer group"),
        "geography": _enum(GEOGRAPHIES, "Main market"),
        "tags": _list("2-5 lowercase topic tags, e.g. 'accessibility', 'real estate', 'e-invoicing'"),
        "self_score": _int("Your honest 1-10 rating of this opportunity for this founder"),
    }
    return {
        "type": "object", "additionalProperties": False, "required": ["ideas"],
        "properties": {"ideas": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": list(props), "properties": props}}},
    }


def profile_text(profile: dict) -> str:
    labels = {"about": "About", "languages": "Languages", "skills": "Skills", "assets": "Assets",
              "hours_per_week": "Hours per week for a new idea", "avoid": "Avoid"}
    return "\n".join(f"- {labels.get(k, k)}: {v}" for k, v in profile.items() if str(v).strip())
