"""
Fuzzy Matching Module for File Operations

Implements a multi-strategy matching chain to robustly find and replace text,
accommodating variations in whitespace, indentation, and escaping common
in LLM-generated code.

The 9-strategy chain (inspired by OpenCode), tried in order:
1. Exact match - Direct string comparison
2. Line-trimmed - Strip leading/trailing whitespace per line
3. Whitespace normalized - Collapse multiple spaces/tabs to single space
4. Indentation flexible - Ignore indentation differences entirely
5. Escape normalized - Convert \\n literals to actual newlines
6. Trimmed boundary - Trim first/last line whitespace only
7. Block anchor - Match first+last lines, use similarity for middle
8. Context-aware - 50% line similarity threshold

Multi-occurrence matching is handled via the replace_all flag.

Usage:
    from tools.fuzzy_match import fuzzy_find_and_replace
    
    new_content, match_count, strategy, error = fuzzy_find_and_replace(
        content="def foo():\\n    pass",
        old_string="def foo():",
        new_string="def bar():",
        replace_all=False
    )
"""
import re
from typing import Tuple, Optional, List, Callable
from difflib import SequenceMatcher
UNICODE_MAP = {'“': '"', '”': '"', '‘': "'", '’': "'", '—': '--', '–': '-', '…': '...', '\xa0': ' ', '−': '-', '\u2000': ' ', '\u2001': ' ', '\u2002': ' ', '\u2003': ' ', '\u2004': ' ', '\u2005': ' ', '\u2006': ' ', '\u2007': ' ', '\u2008': ' ', '\u2009': ' ', '\u200a': ' ', '\u202f': ' ', '\u205f': ' ', '\u3000': ' '}

def _unicode_normalize(text: str) -> str:
    """Normalizes Unicode characters to their standard ASCII equivalents."""
    for char, repl in UNICODE_MAP.items():
        text = text.replace(char, repl)
    return text

def is_already_applied(content: str, old_string: str, new_string: str) -> bool:
    """Return True when the requested edit is already present in the file.

    Production trajectory mining shows the most common patch failure is a
    re-send of an edit that already landed (old_string == new_string, or
    old_string gone while new_string is present) — the model's intent is
    "make the file contain this text", and it already does. Callers use
    this to convert those errors into an explicit success-shaped no-op so
    the model moves on instead of re-reading and re-patching.

    Deliberately conservative:
    - new_string must be non-trivial (>= 8 chars stripped) — a tiny target
      matching by coincidence must not mask a genuine typo'd edit;
    - new_string must appear EXACTLY in the content (no fuzzy matching —
      approximate presence is not proof the edit landed);
    - when old_string differs from new_string, old_string must be GONE
      (still-present old text means the edit is at best half-applied).
    """
    if not new_string or len(new_string.strip()) < 8:
        return False
    if new_string not in content:
        return False
    if old_string == new_string:
        return True
    return old_string not in content

def _format_match_locations(content: str, matches: List[Tuple[int, int]], cap: int=5) -> str:
    """Render up to ``cap`` match positions as 'L<line>: <snippet>' rows.

    Gives the model the information it needs to disambiguate an ambiguous
    old_string in ONE follow-up (add neighboring context, or choose
    replace_all) instead of re-reading the file to find the occurrences.
    """
    rows = []
    for start, _end in matches[:cap]:
        line_no = content.count('\n', 0, start) + 1
        line_start = content.rfind('\n', 0, start) + 1
        line_end = content.find('\n', line_start)
        if line_end == -1:
            line_end = len(content)
        snippet = content[line_start:line_end].strip()
        if len(snippet) > 80:
            snippet = snippet[:77] + '...'
        rows.append(f'  L{line_no}: {snippet}')
    extra = len(matches) - cap
    if extra > 0:
        rows.append(f'  ... and {extra} more')
    return '\n'.join(rows)

def fuzzy_find_and_replace(content: str, old_string: str, new_string: str, replace_all: bool=False) -> Tuple[str, int, Optional[str], Optional[str]]:
    """
    Find and replace text using a chain of increasingly fuzzy matching strategies.

    Args:
        content: The file content to search in
        old_string: The text to find
        new_string: The replacement text
        replace_all: If True, replace all occurrences; if False, require uniqueness

    Returns:
        Tuple of (new_content, match_count, strategy_name, error_message)
        - If successful: (modified_content, number_of_replacements, strategy_used, None)
        - If failed: (original_content, 0, None, error_description)
    """
    if not old_string:
        return (content, 0, None, 'old_string cannot be empty')
    if not old_string.strip():
        return (content, 0, None, 'old_string is only whitespace — provide non-blank text to match')
    if old_string == new_string:
        return (content, 0, None, 'old_string and new_string are identical')
    strategies: List[Tuple[str, Callable]] = [('exact', _strategy_exact), ('line_trimmed', _strategy_line_trimmed), ('whitespace_normalized', _strategy_whitespace_normalized), ('indentation_flexible', _strategy_indentation_flexible), ('escape_normalized', _strategy_escape_normalized), ('trimmed_boundary', _strategy_trimmed_boundary), ('unicode_normalized', _strategy_unicode_normalized), ('block_anchor', _strategy_block_anchor), ('context_aware', _strategy_context_aware)]
    _SIMILARITY_STRATEGIES = {'block_anchor', 'context_aware'}
    for strategy_name, strategy_fn in strategies:
        matches = strategy_fn(content, old_string)
        if matches:
            if len(matches) > 1 and (not replace_all):
                locations = _format_match_locations(content, matches)
                return (content, 0, None, f'Found {len(matches)} matches for old_string. Provide more context to make it unique, or use replace_all=True. Matches:\n{locations}')
            if replace_all and len(matches) > 1 and (strategy_name in _SIMILARITY_STRATEGIES):
                return (content, 0, None, f"Found {len(matches)} approximate matches via the '{strategy_name}' strategy; replace_all only applies to exact matches. Provide the precise text (whitespace included) so an exact/line-trimmed match can be made.")
            if strategy_name != 'exact':
                drift_err = _detect_escape_drift(content, matches, old_string, new_string)
                if drift_err:
                    return (content, 0, None, drift_err)
            effective_new = _maybe_unescape_new_string(new_string, content, matches)
            if strategy_name == 'unicode_normalized':
                effective_new = _preserve_unicode_in_replacement(content, matches, old_string, effective_new)
            new_content = _apply_replacements(content, matches, effective_new, old_string=old_string if strategy_name != 'exact' else None)
            return (new_content, len(matches), strategy_name, None)
    return (content, 0, None, 'Could not find a match for old_string in the file')

def _detect_escape_drift(content: str, matches: List[Tuple[int, int]], old_string: str, new_string: str) -> Optional[str]:
    """Detect tool-call escape-drift artifacts in new_string.

    Looks for ``\\'`` or ``\\"`` sequences that are present in both
    old_string and new_string (i.e. the model copy-pasted them as "context"
    it intended to preserve) but don't exist in the matched region of the
    file. That pattern indicates the transport layer inserted spurious
    shell-style escapes around apostrophes or quotes — writing new_string
    verbatim would literally insert ``\\'`` into source code.

    Returns an error string if drift is detected, None otherwise.
    """
    if "\\'" not in new_string and '\\"' not in new_string:
        return None
    matched_regions = ''.join((content[start:end] for start, end in matches))
    for suspect in ("\\'", '\\"'):
        if suspect in new_string and suspect in old_string and (suspect not in matched_regions):
            plain = suspect[1]
            return f'Escape-drift detected: old_string and new_string contain the literal sequence {suspect!r} but the matched region of the file does not. This is almost always a tool-call serialization artifact where an apostrophe or quote got prefixed with a spurious backslash. Re-read the file with read_file and pass old_string/new_string without backslash-escaping {plain!r} characters.'
    return None

def _leading_whitespace(line: str) -> str:
    """Return the leading whitespace prefix of a line (spaces/tabs)."""
    i = 0
    while i < len(line) and line[i] in (' ', '\t'):
        i += 1
    return line[:i]

def _first_meaningful_line(text: str) -> Optional[str]:
    """Return the first line of ``text`` that has any non-whitespace content.

    Returns ``None`` if no such line exists (text is empty or all whitespace).
    """
    for line in text.split('\n'):
        if line.strip():
            return line
    return None

def _reindent_replacement(file_region: str, old_string: str, new_string: str) -> str:
    """Adjust ``new_string`` so its indentation matches ``file_region``.

    Used after a non-exact fuzzy match: the LLM may have sent old_string and
    new_string with a different indent than the file actually has (e.g.
    2-space indent in tool args vs 4-space indent on disk). The fuzzy
    strategy successfully matched anyway, but writing ``new_string`` verbatim
    would corrupt the file's indentation.

    Approach:

    1. For each non-blank line in ``new_string``, compute its indent
       *relative* to the shallowest non-blank line of ``old_string`` (the
       LLM's base indent).
    2. Anchor that relative indent onto the file's actual base indent (the
       leading whitespace of the file_region's first non-blank line).
    3. Re-emit each non-blank line as ``file_base + (line_indent - llm_base)``.

    Blank lines and lines less-indented than the LLM's base are anchored
    directly to the file's base indent.

    No-op cases (returns ``new_string`` unchanged):
    - file_region or old_string has no meaningful line
    - LLM base indent equals file base indent
    - new_string is empty
    """
    if not new_string:
        return new_string
    old_first = _first_meaningful_line(old_string)
    file_first = _first_meaningful_line(file_region)
    if old_first is None or file_first is None:
        return new_string
    old_indent = _leading_whitespace(old_first)
    file_indent = _leading_whitespace(file_first)
    if old_indent == file_indent:
        return new_string
    out_lines: List[str] = []
    for line in new_string.split('\n'):
        if not line.strip():
            out_lines.append(line)
            continue
        line_indent = _leading_whitespace(line)
        if line_indent.startswith(old_indent):
            remainder = line[len(old_indent):]
            out_lines.append(file_indent + remainder)
        else:
            out_lines.append(file_indent + line.lstrip(' \t'))
    return '\n'.join(out_lines)

def _maybe_unescape_new_string(new_string: str, content: str, matches: List[Tuple[int, int]]) -> str:
    """Conditionally unescape ``\\t``/``\\r`` in new_string.

    LLMs frequently send the two-character sequences ``\\t`` (backslash + t)
    and ``\\r`` (backslash + r) inside JSON tool-call arguments where they
    meant a real tab or carriage-return byte. Writing the string verbatim
    corrupts tab-indented files with literal backslash-letter pairs.

    The unescape is only applied per-sequence when the *matched region of
    the file* actually contains the corresponding control character — that
    is, we only convert ``\\t`` -> tab when the file region we're replacing
    contains a real tab byte. Files that legitimately contain the literal
    two-character string ``"\\t"`` (e.g. a Python source line that defines
    ``sep = "\\t"``) get a backslash+t in the matched region instead of a
    tab, so we leave new_string alone.

    ``\\n`` is intentionally excluded: newlines serialize correctly through
    JSON and rewriting backslash-n would corrupt escape sequences in
    string literals far more often than it would help.
    """
    if '\\t' not in new_string and '\\r' not in new_string:
        return new_string
    matched_regions = ''.join((content[start:end] for start, end in matches))
    out = new_string
    if '\\t' in out and '\t' in matched_regions:
        out = out.replace('\\t', '\t')
    if '\\r' in out and '\r' in matched_regions:
        out = out.replace('\\r', '\r')
    return out

def _preserve_unicode_in_replacement(content: str, matches: List[Tuple[int, int]], old_string: str, new_string: str) -> str:
    """Preserve Unicode characters from the file in the replacement string.

    When strategy 7 (unicode_normalized) matched, the file has Unicode
    characters (em-dashes, smart quotes, ellipsis, non-breaking spaces)
    but old_string/new_string from the LLM are ASCII equivalents.
    Writing new_string verbatim would silently corrupt the file's
    Unicode — em-dashes become two hyphens, smart quotes become
    straight quotes.

    This function aligns the replacement with the file's actual Unicode
    by diffing old_string→new_string and applying only the actual edits
    to the file's original text, preserving Unicode for unchanged portions.
    """
    file_region = ''.join((content[start:end] for start, end in matches))
    norm_old = _unicode_normalize(old_string)
    norm_file = _unicode_normalize(file_region)
    if norm_old != norm_file:
        return new_string
    file_orig_to_norm = _build_orig_to_norm_map(file_region)
    file_norm_to_orig: dict[int, int] = {}
    for orig_pos, np in enumerate(file_orig_to_norm[:-1]):
        if np not in file_norm_to_orig:
            file_norm_to_orig[np] = orig_pos
    sm = SequenceMatcher(None, norm_old, new_string)
    opcodes = sm.get_opcodes()
    result_parts: List[str] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == 'equal':
            orig_start = file_norm_to_orig.get(i1, 0)
            orig_end = orig_start
            while orig_end < len(file_region) and file_orig_to_norm[orig_end] < i2:
                orig_end += 1
            result_parts.append(file_region[orig_start:orig_end])
        elif tag == 'replace':
            result_parts.append(new_string[j1:j2])
        elif tag == 'delete':
            pass
        elif tag == 'insert':
            result_parts.append(new_string[j1:j2])
    return ''.join(result_parts)

def _apply_replacements(content: str, matches: List[Tuple[int, int]], new_string: str, old_string: Optional[str]=None) -> str:
    """
    Apply replacements at the given positions.

    Args:
        content: Original content
        matches: List of (start, end) positions to replace
        new_string: Replacement text
        old_string: When non-None, signals that the match came from a
            non-exact fuzzy strategy; ``new_string`` is re-indented to
            match the file's actual indentation before substitution.

    Returns:
        Content with replacements applied
    """
    sorted_matches = sorted(matches, key=lambda x: x[0], reverse=True)
    result = content
    for start, end in sorted_matches:
        if old_string is not None:
            file_region = content[start:end]
            adjusted = _reindent_replacement(file_region, old_string, new_string)
        else:
            adjusted = new_string
        result = result[:start] + adjusted + result[end:]
    return result

def _strategy_exact(content: str, pattern: str) -> List[Tuple[int, int]]:
    """Strategy 1: Exact string match."""
    matches = []
    start = 0
    while True:
        pos = content.find(pattern, start)
        if pos == -1:
            break
        matches.append((pos, pos + len(pattern)))
        start = pos + len(pattern)
    return matches

def _strategy_line_trimmed(content: str, pattern: str) -> List[Tuple[int, int]]:
    """
    Strategy 2: Match with line-by-line whitespace trimming.
    
    Strips leading/trailing whitespace from each line before matching.
    """
    pattern_lines = [line.strip() for line in pattern.split('\n')]
    pattern_normalized = '\n'.join(pattern_lines)
    content_lines = content.split('\n')
    content_normalized_lines = [line.strip() for line in content_lines]
    return _find_normalized_matches(content, content_lines, content_normalized_lines, pattern, pattern_normalized)

def _strategy_whitespace_normalized(content: str, pattern: str) -> List[Tuple[int, int]]:
    """
    Strategy 3: Collapse multiple whitespace to single space.
    """

    def normalize(s):
        return re.sub('[ \\t]+', ' ', s)
    pattern_normalized = normalize(pattern)
    content_normalized = normalize(content)
    matches_in_normalized = _strategy_exact(content_normalized, pattern_normalized)
    if not matches_in_normalized:
        return []
    return _map_normalized_positions(content, content_normalized, matches_in_normalized)

def _strategy_indentation_flexible(content: str, pattern: str) -> List[Tuple[int, int]]:
    """
    Strategy 4: Ignore indentation differences entirely.
    
    Strips all leading whitespace from lines before matching.
    """
    content_lines = content.split('\n')
    content_stripped_lines = [line.lstrip() for line in content_lines]
    pattern_lines = [line.lstrip() for line in pattern.split('\n')]
    return _find_normalized_matches(content, content_lines, content_stripped_lines, pattern, '\n'.join(pattern_lines))

def _strategy_escape_normalized(content: str, pattern: str) -> List[Tuple[int, int]]:
    """
    Strategy 5: Convert escape sequences to actual characters.
    
    Handles \\n -> newline, \\t -> tab, etc.
    """

    def unescape(s):
        return s.replace('\\n', '\n').replace('\\t', '\t').replace('\\r', '\r')
    pattern_unescaped = unescape(pattern)
    if pattern_unescaped == pattern:
        return []
    return _strategy_exact(content, pattern_unescaped)

def _strategy_trimmed_boundary(content: str, pattern: str) -> List[Tuple[int, int]]:
    """
    Strategy 6: Trim whitespace from first and last lines only.
    
    Useful when the pattern boundaries have whitespace differences.
    """
    pattern_lines = pattern.split('\n')
    if not pattern_lines:
        return []
    pattern_lines[0] = pattern_lines[0].strip()
    if len(pattern_lines) > 1:
        pattern_lines[-1] = pattern_lines[-1].strip()
    modified_pattern = '\n'.join(pattern_lines)
    content_lines = content.split('\n')
    matches = []
    pattern_line_count = len(pattern_lines)
    for i in range(len(content_lines) - pattern_line_count + 1):
        block_lines = content_lines[i:i + pattern_line_count]
        check_lines = block_lines.copy()
        check_lines[0] = check_lines[0].strip()
        if len(check_lines) > 1:
            check_lines[-1] = check_lines[-1].strip()
        if '\n'.join(check_lines) == modified_pattern:
            start_pos, end_pos = _calculate_line_positions(content_lines, i, i + pattern_line_count, len(content))
            matches.append((start_pos, end_pos))
    return matches

def _build_orig_to_norm_map(original: str) -> List[int]:
    """Build a list mapping each original character index to its normalized index.

    Because UNICODE_MAP replacements may expand characters (e.g. em-dash → '--',
    ellipsis → '...'), the normalised string can be longer than the original.
    This map lets us convert positions in the normalised string back to the
    corresponding positions in the original string.

    Returns a list of length ``len(original) + 1``; entry ``i`` is the
    normalised index that character ``i`` maps to.
    """
    result: List[int] = []
    norm_pos = 0
    for char in original:
        result.append(norm_pos)
        repl = UNICODE_MAP.get(char)
        norm_pos += len(repl) if repl is not None else 1
    result.append(norm_pos)
    return result

def _map_positions_norm_to_orig(orig_to_norm: List[int], norm_matches: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Convert (start, end) positions in the normalised string to original positions."""
    norm_to_orig_start: dict[int, int] = {}
    for orig_pos, norm_pos in enumerate(orig_to_norm[:-1]):
        if norm_pos not in norm_to_orig_start:
            norm_to_orig_start[norm_pos] = orig_pos
    results: List[Tuple[int, int]] = []
    orig_len = len(orig_to_norm) - 1
    for norm_start, norm_end in norm_matches:
        if norm_start not in norm_to_orig_start:
            continue
        orig_start = norm_to_orig_start[norm_start]
        orig_end = orig_start
        while orig_end < orig_len and orig_to_norm[orig_end] < norm_end:
            orig_end += 1
        results.append((orig_start, orig_end))
    return results

def _strategy_unicode_normalized(content: str, pattern: str) -> List[Tuple[int, int]]:
    """Strategy 7: Unicode normalisation.

    Normalises smart quotes, em/en-dashes, ellipsis, and non-breaking spaces
    to their ASCII equivalents in both *content* and *pattern*, then runs
    exact and line_trimmed matching on the normalised copies.

    Positions are mapped back to the *original* string via
    ``_build_orig_to_norm_map`` — necessary because some UNICODE_MAP
    replacements expand a single character into multiple ASCII characters,
    making a naïve position copy incorrect.
    """
    norm_pattern = _unicode_normalize(pattern)
    norm_content = _unicode_normalize(content)
    if norm_content == content and norm_pattern == pattern:
        return []
    norm_matches = _strategy_exact(norm_content, norm_pattern)
    if not norm_matches:
        norm_matches = _strategy_line_trimmed(norm_content, norm_pattern)
    if not norm_matches:
        return []
    orig_to_norm = _build_orig_to_norm_map(content)
    return _map_positions_norm_to_orig(orig_to_norm, norm_matches)

def _strategy_block_anchor(content: str, pattern: str) -> List[Tuple[int, int]]:
    """
    Strategy 8: Match by anchoring on first and last lines.
    Adjusted with permissive thresholds and unicode normalization.
    """
    norm_pattern = _unicode_normalize(pattern)
    norm_content = _unicode_normalize(content)
    pattern_lines = norm_pattern.split('\n')
    if len(pattern_lines) < 2:
        return []
    first_line = pattern_lines[0].strip()
    last_line = pattern_lines[-1].strip()
    norm_content_lines = norm_content.split('\n')
    orig_content_lines = content.split('\n')
    pattern_line_count = len(pattern_lines)
    potential_matches = []
    for i in range(len(norm_content_lines) - pattern_line_count + 1):
        if norm_content_lines[i].strip() == first_line and norm_content_lines[i + pattern_line_count - 1].strip() == last_line:
            potential_matches.append(i)
    matches = []
    candidate_count = len(potential_matches)
    threshold = 0.5 if candidate_count == 1 else 0.7
    for i in potential_matches:
        if pattern_line_count <= 2:
            similarity = 1.0
        else:
            content_middle = '\n'.join(norm_content_lines[i + 1:i + pattern_line_count - 1])
            pattern_middle = '\n'.join(pattern_lines[1:-1])
            similarity = SequenceMatcher(None, content_middle, pattern_middle).ratio()
        if similarity >= threshold:
            start_pos, end_pos = _calculate_line_positions(orig_content_lines, i, i + pattern_line_count, len(content))
            matches.append((start_pos, end_pos))
    return matches

def _strategy_context_aware(content: str, pattern: str) -> List[Tuple[int, int]]:
    """
    Strategy 9 (last resort): anchored line-by-line similarity.

    Only considers blocks whose first AND last lines closely match the
    pattern's first/last lines (an anchor pre-filter), then requires EVERY
    non-blank pattern line to be highly similar (>=0.80) to the aligned
    content line. The anchor filter keeps this from being an O(file x pattern)
    scan on every miss, and the all-lines requirement stops a single
    coincidental line-match from silently replacing an unrelated block
    (the old 50%-of-lines threshold accepted half-garbage patterns and
    destroyed the non-matching lines).
    """
    pattern_lines = pattern.split('\n')
    content_lines = content.split('\n')
    if not pattern_lines:
        return []
    pattern_line_count = len(pattern_lines)
    if pattern_line_count > len(content_lines):
        return []
    first_pat = pattern_lines[0].strip()
    last_pat = pattern_lines[-1].strip()
    ANCHOR_THRESHOLD = 0.8

    def _sim(a: str, b: str) -> float:
        if a == b:
            return 1.0
        return SequenceMatcher(None, a, b).ratio()
    matches = []
    for i in range(len(content_lines) - pattern_line_count + 1):
        block_lines = content_lines[i:i + pattern_line_count]
        if _sim(first_pat, block_lines[0].strip()) < ANCHOR_THRESHOLD:
            continue
        if _sim(last_pat, block_lines[-1].strip()) < ANCHOR_THRESHOLD:
            continue
        all_match = True
        for p_line, c_line in zip(pattern_lines, block_lines):
            p_stripped = p_line.strip()
            if not p_stripped:
                continue
            if _sim(p_stripped, c_line.strip()) < 0.8:
                all_match = False
                break
        if all_match:
            start_pos, end_pos = _calculate_line_positions(content_lines, i, i + pattern_line_count, len(content))
            matches.append((start_pos, end_pos))
    return matches

def _calculate_line_positions(content_lines: List[str], start_line: int, end_line: int, content_length: int) -> Tuple[int, int]:
    """Calculate start and end character positions from line indices.

    Args:
        content_lines: List of lines (without newlines)
        start_line: Starting line index (0-based)
        end_line: Ending line index (exclusive, 0-based)
        content_length: Total length of the original content string

    Returns:
        Tuple of (start_pos, end_pos) in the original content
    """
    start_pos = sum((len(line) + 1 for line in content_lines[:start_line]))
    end_pos = sum((len(line) + 1 for line in content_lines[:end_line])) - 1
    end_pos = min(content_length, end_pos)
    return (start_pos, end_pos)

def _find_normalized_matches(content: str, content_lines: List[str], content_normalized_lines: List[str], pattern: str, pattern_normalized: str) -> List[Tuple[int, int]]:
    """
    Find matches in normalized content and map back to original positions.
    
    Args:
        content: Original content string
        content_lines: Original content split by lines
        content_normalized_lines: Normalized content lines
        pattern: Original pattern
        pattern_normalized: Normalized pattern
    
    Returns:
        List of (start, end) positions in the original content
    """
    pattern_norm_lines = pattern_normalized.split('\n')
    num_pattern_lines = len(pattern_norm_lines)
    matches = []
    for i in range(len(content_normalized_lines) - num_pattern_lines + 1):
        block = '\n'.join(content_normalized_lines[i:i + num_pattern_lines])
        if block == pattern_normalized:
            start_pos, end_pos = _calculate_line_positions(content_lines, i, i + num_pattern_lines, len(content))
            matches.append((start_pos, end_pos))
    return matches

def _map_normalized_positions(original: str, normalized: str, normalized_matches: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """
    Map positions from normalized string back to original.
    
    This is a best-effort mapping that works for whitespace normalization.
    """
    if not normalized_matches:
        return []
    orig_to_norm = []
    orig_idx = 0
    norm_idx = 0
    while orig_idx < len(original) and norm_idx < len(normalized):
        if original[orig_idx] == normalized[norm_idx]:
            orig_to_norm.append(norm_idx)
            orig_idx += 1
            norm_idx += 1
        elif original[orig_idx] in ' \t' and normalized[norm_idx] == ' ':
            orig_to_norm.append(norm_idx)
            orig_idx += 1
            if orig_idx < len(original) and original[orig_idx] not in ' \t':
                norm_idx += 1
        elif original[orig_idx] in ' \t':
            orig_to_norm.append(norm_idx)
            orig_idx += 1
        else:
            orig_to_norm.append(norm_idx)
            orig_idx += 1
    while orig_idx < len(original):
        orig_to_norm.append(len(normalized))
        orig_idx += 1
    norm_to_orig_start = {}
    norm_to_orig_end = {}
    for orig_pos, norm_pos in enumerate(orig_to_norm):
        if norm_pos not in norm_to_orig_start:
            norm_to_orig_start[norm_pos] = orig_pos
        norm_to_orig_end[norm_pos] = orig_pos
    original_matches = []
    for norm_start, norm_end in normalized_matches:
        if norm_start in norm_to_orig_start:
            orig_start = norm_to_orig_start[norm_start]
        else:
            orig_start = min((i for i, n in enumerate(orig_to_norm) if n >= norm_start))
        if norm_end - 1 in norm_to_orig_end:
            orig_end = norm_to_orig_end[norm_end - 1] + 1
        else:
            orig_end = orig_start + (norm_end - norm_start)
        if norm_end < len(normalized) and normalized[norm_end - 1] == ' ':
            while orig_end < len(original) and original[orig_end] in ' \t':
                orig_end += 1
        original_matches.append((orig_start, min(orig_end, len(original))))
    return original_matches

def _visualize_whitespace(line: str) -> str:
    """Render leading whitespace visibly (→ = tab, · = space).

    Only the leading run is visualized — interior spacing is rarely the
    culprit and full visualization makes lines unreadable.
    """
    i = 0
    prefix = []
    while i < len(line) and line[i] in (' ', '\t'):
        prefix.append('→' if line[i] == '\t' else '·')
        i += 1
    return ''.join(prefix) + line[i:]

def find_closest_lines(old_string: str, content: str, context_lines: int=2, max_results: int=3) -> str:
    """Find lines in content most similar to old_string for "did you mean?" feedback.

    Returns a formatted string showing the closest matching lines with context,
    or empty string if no useful match is found.
    """
    if not old_string or not content:
        return ''
    old_lines = old_string.splitlines()
    content_lines = content.splitlines()
    if not old_lines or not content_lines:
        return ''
    anchor = old_lines[0].strip()
    if not anchor:
        candidates = [l.strip() for l in old_lines if l.strip()]
        if not candidates:
            return ''
        anchor = candidates[0]
    scored = []
    for i, line in enumerate(content_lines):
        stripped = line.strip()
        if not stripped:
            continue
        ratio = SequenceMatcher(None, anchor, stripped).ratio()
        if ratio > 0.3:
            scored.append((ratio, i))
    if not scored:
        return ''
    scored.sort(key=lambda x: -x[0])
    top = scored[:max_results]
    parts = []
    seen_ranges = set()
    for _, line_idx in top:
        start = max(0, line_idx - context_lines)
        end = min(len(content_lines), line_idx + len(old_lines) + context_lines)
        key = (start, end)
        if key in seen_ranges:
            continue
        seen_ranges.add(key)
        snippet = '\n'.join((f'{start + j + 1:4d}| {content_lines[start + j]}' for j in range(end - start)))
        parts.append(snippet)
    if not parts:
        return ''
    result = '\n---\n'.join(parts)
    best_line = content_lines[top[0][1]]
    if best_line.strip() == anchor and best_line != old_lines[0]:
        result += f"\n\nWhitespace difference detected (→ = tab, · = space):\n  file has: {_visualize_whitespace(best_line)}\n  you sent: {_visualize_whitespace(old_lines[0])}\nUse the exact whitespace shown in 'file has'."
    return result

def format_no_match_hint(error: Optional[str], match_count: int, old_string: str, content: str) -> str:
    """Return a '\\n\\nDid you mean...' snippet for plain no-match errors.

    Gated so the hint only fires for actual "old_string not found" failures.
    Ambiguous-match ("Found N matches"), escape-drift, and identical-strings
    errors all have ``match_count == 0`` but a "did you mean?" snippet would
    be misleading — those failed for unrelated reasons.

    Returns an empty string when there's nothing useful to append.
    """
    if match_count != 0:
        return ''
    if not error or not error.startswith('Could not find'):
        return ''
    hint = find_closest_lines(old_string, content)
    if not hint:
        return ''
    return '\n\nDid you mean one of these sections?\n' + hint