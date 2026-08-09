"""Default SOUL.md template seeded into DUCK_AGENT_HOME on first run."""
DEFAULT_SOUL_MD = 'You are Duck Agent, an intelligent AI assistant created by Nous Research. You are helpful, knowledgeable, and direct. You assist users with a wide range of tasks including answering questions, writing and editing code, analyzing information, creative work, and executing actions via your tools. You communicate clearly, admit uncertainty when appropriate, and prioritize being genuinely useful over being verbose unless otherwise directed below. Be targeted and efficient in your exploration and investigations.'
_LEGACY_TEMPLATE_SOULS = ('# Duck Agent Persona\n\n<!--\nThis file defines the agent\'s personality and tone.\nThe agent will embody whatever you write here.\nEdit this to customize how Duck Agent communicates with you.\n\nExamples:\n  - "You are a warm, playful assistant who uses kaomoji occasionally."\n  - "You are a concise technical expert. No fluff, just facts."\n  - "You speak like a friendly coworker who happens to know everything."\n\nThis file is loaded fresh each message -- no restart needed.\nDelete the contents (or this file) to use the default personality.\n-->', "# Duck Agent Persona\n\n<!--\nThis file defines the agent's personality and tone.\nThe agent will embody whatever you write here.\nEdit this to customize how Duck Agent communicates with you.\n\nThis file is loaded fresh each message -- no restart needed.\nDelete the contents (or this file) to use the default personality.\n-->")

def _normalize_soul(text: str) -> str:
    """Normalize SOUL.md content for legacy-template comparison."""
    return text.replace('\r\n', '\n').replace('\r', '\n').lstrip('\ufeff').strip()

def is_legacy_template_soul(text: str) -> bool:
    """True if ``text`` is an old empty-template SOUL.md (no user persona).

    Older installers seeded a comment-only scaffold instead of DEFAULT_SOUL_MD,
    which shadowed the runtime default and left users with no persona. A file
    matching one of those known scaffolds carries zero user intent and is safe
    to upgrade in place. Any deviation (the user typed a persona, even one
    character outside the comment) makes this return False.
    """
    normalized = _normalize_soul(text)
    return any((normalized == _normalize_soul(t) for t in _LEGACY_TEMPLATE_SOULS))