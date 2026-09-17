"""The assistant's system prompt — assembled from the shipped playbook.

``skill/SKILL.md`` is the operating manual this server already publishes at
``/skill`` for external agents. The built-in assistant reads the same file
at request time, so a change to the skill (aisle vocabulary, quantity rules,
ask-vs-act judgement) reaches the embedded agent on the next turn without
any second prompt being maintained. The preamble does only what an external
agent's connection does: names the tools as live, fixes the date, and
closes the one gap an embedded agent has — there is no API base URL to
remember and no skill to install, because both are already inside.
"""

from datetime import date

from app.routers.skill import load_skill_file, playbook_version

_PREAMBLE = """You are Meals' built-in assistant, inside the Meals app, acting for the \
signed-in user's household. Everything below is one household's own data; your tools can \
reach only what this user may reach, and you must never suggest otherwise.

Your tools are provided in this conversation — call them directly. The manual below is \
written for external assistants that use REST or MCP: wherever it says to call a tool by \
name, you have that tool; wherever it names an API endpoint or URL, your equivalent tool \
does the job and you should never construct URLs. Ignore instructions about installing or \
re-fetching the skill — it is loaded fresh on every conversation, always current.

Today is {today}. Answer in the language the user writes in.
"""


def build_system_prompt(today: date, base_url: str) -> str:
    """The full prompt: preamble, then the playbook, with ``{{API_URL}}``
    filled the same way the /skill endpoint fills it (so the URLs the manual
    does mention are real rather than placeholders)."""
    try:
        skill = load_skill_file("SKILL.md")
    except Exception:  # noqa: BLE001 — a build without the skill still chats
        skill = ""
    version = playbook_version()
    version_note = f"(playbook v{version}, loaded fresh this conversation)" if version else ""
    rendered = skill.replace("{{API_URL}}", base_url)
    return _PREAMBLE.format(today=today.isoformat()) + version_note + "\n\n" + rendered
