import re
import sys
from pathlib import Path


MARKER_RE = re.compile(r"[ÃÂâðï]")
LITERAL_REPLACEMENTS = {
    "âœï¸": "✏️",
    "âš ï¸": "⚠️",
    "â˜€ï¸": "☀️",
    "â›ˆï¸": "⛈️",
    "â„ï¸": "❄️",
    "â˜ï¸": "☁️",
    "â›…": "⛅",
    "âœ…": "✅",
    "â€¢": "•",
    "â€”": "—",
    "â€“": "–",
    "â€¦": "…",
    "â€™": "’",
    "â€œ": "“",
    "â€": "”",
    "â€˜": "‘",
    "â†’": "→",
    "â†“": "↓",
    "â†‘": "↑",
    "â†": "←",
    "âœ“": "✓",
    "âœ•": "✕",
    "â³": "⏳",
    "âš¡": "⚡",
    "â–¾": "▾",
    "â–¸": "▸",
    "â—": "●",
    "â‚¬": "€",
    "Â·": "·",
    "Â°": "°",
    "Â©": "©",
    "Ã‰": "É",
    "Ã€": "À",
    "ÃŽ": "Î",
    "Ã©": "é",
    "Ã¨": "è",
    "Ã ": "à",
    "Ã¹": "ù",
    "Ãª": "ê",
    "Ã«": "ë",
    "Ã®": "î",
    "Ã¯": "ï",
    "Ã´": "ô",
    "Ã¶": "ö",
    "Ã»": "û",
    "Ã§": "ç",
    "Ã¢": "â",
    "ðŸ—ºï¸": "🗺️",
    "ðŸ–¨ï¸": "🖨️",
    "ðŸŒ¤ï¸": "🌤️",
    "ðŸŒ¦ï¸": "🌦️",
    "ðŸŒ§ï¸": "🌧️",
    "ðŸŒ«ï¸": "🌫️",
    "ðŸŒ¡ï¸": "🌡️",
    "ðŸŒ¿": "🌿",
    "ðŸŒ±": "🌱",
    "ðŸŒ¾": "🌾",
    "ðŸ“¥": "📥",
    "ðŸ“„": "📄",
    "ðŸ“Š": "📊",
    "ðŸ“…": "📅",
    "ðŸ“¦": "📦",
    "ðŸ“": "📝",
    "ðŸ““": "📓",
    "ðŸ“ˆ": "📈",
    "ðŸ”": "🔍",
    "ðŸ—‘": "🗑",
    "ðŸ—º": "🗺",
    "ðŸ›°": "🛰",
    "ðŸ§¾": "🧾",
    "ðŸ¥”": "🥔",
    "ðŸ‘¥": "👥",
    "ðŸ’¾": "💾",
    "ðŸ’¶": "💶",
    "ï¸": "️",
}


def bad_score(value: str) -> int:
    return sum(value.count(ch) for ch in ("Ã", "Â", "â", "ð", "ï", "�"))


def repair_text(value: str) -> str:
    current = value
    while MARKER_RE.search(current):
        try:
            candidate = current.encode("cp1252").decode("utf-8")
        except UnicodeError:
            break
        if bad_score(candidate) < bad_score(current):
            current = candidate
            continue
        break
    return current


def repair_html_chunk(html: str) -> str:
    html = re.sub(
        r'(\b(?:placeholder|title|alt|aria-label|content)=)(["\'])(.*?[ÃÂâðï].*?)\2',
        lambda match: f"{match.group(1)}{match.group(2)}{repair_text(match.group(3))}{match.group(2)}",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    html = re.sub(
        r">([^<]*[ÃÂâðï][^<]*)<",
        lambda match: ">" + repair_text(match.group(1)) + "<",
        html,
    )
    return html


def repair_single_and_double_quoted_strings(code: str) -> str:
    code = re.sub(
        r"'((?:\\.|[^'\\])*)'",
        lambda match: "'" + repair_text(match.group(1)) + "'",
        code,
        flags=re.DOTALL,
    )
    code = re.sub(
        r'"((?:\\.|[^"\\])*)"',
        lambda match: '"' + repair_text(match.group(1)) + '"',
        code,
        flags=re.DOTALL,
    )
    return code


def skip_js_string(code: str, index: int, quote: str) -> int:
    index += 1
    while index < len(code):
        if code[index] == "\\":
            index += 2
            continue
        if code[index] == quote:
            return index + 1
        index += 1
    return index


def skip_js_block_comment(code: str, index: int) -> int:
    end = code.find("*/", index + 2)
    return len(code) if end == -1 else end + 2


def skip_js_line_comment(code: str, index: int) -> int:
    end = code.find("\n", index + 2)
    return len(code) if end == -1 else end


def skip_template_literal(code: str, index: int) -> int:
    index += 1
    while index < len(code):
        if code[index] == "\\":
            index += 2
            continue
        if code[index] == "`":
            return index + 1
        if code[index] == "$" and index + 1 < len(code) and code[index + 1] == "{":
            index = skip_js_expression(code, index + 2)
            continue
        index += 1
    return index


def skip_js_expression(code: str, index: int) -> int:
    depth = 1
    while index < len(code) and depth > 0:
        if code.startswith("//", index):
            index = skip_js_line_comment(code, index)
            continue
        if code.startswith("/*", index):
            index = skip_js_block_comment(code, index)
            continue

        ch = code[index]
        if ch in ("'", '"'):
            index = skip_js_string(code, index, ch)
            continue
        if ch == "`":
            index = skip_template_literal(code, index)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        index += 1
    return index


def repair_template_literals(code: str) -> str:
    out = []
    index = 0

    while index < len(code):
        if code.startswith("//", index):
            end = skip_js_line_comment(code, index)
            out.append(code[index:end])
            index = end
            continue

        if code.startswith("/*", index):
            end = skip_js_block_comment(code, index)
            out.append(code[index:end])
            index = end
            continue

        if code[index] not in ("'", '"', "`"):
            out.append(code[index])
            index += 1
            continue

        if code[index] in ("'", '"'):
            end = skip_js_string(code, index, code[index])
            out.append(code[index:end])
            index = end
            continue

        out.append("`")
        index += 1
        raw = []
        while index < len(code):
            if code[index] == "\\":
                raw.append(code[index:index + 2])
                index += 2
                continue
            if code[index] == "`":
                out.append(repair_html_chunk("".join(raw)))
                out.append("`")
                index += 1
                break
            if code[index] == "$" and index + 1 < len(code) and code[index + 1] == "{":
                out.append(repair_html_chunk("".join(raw)))
                raw = []
                end = skip_js_expression(code, index + 2)
                out.append(code[index:end])
                index = end
                continue
            raw.append(code[index])
            index += 1
        else:
            out.append(repair_html_chunk("".join(raw)))

    return "".join(out)


def repair_js(code: str) -> str:
    return repair_template_literals(repair_single_and_double_quoted_strings(code))


def repair_document(text: str) -> str:
    parts = []
    last_end = 0

    for match in re.finditer(r"(<script\b[^>]*>)(.*?)(</script>)", text, flags=re.IGNORECASE | re.DOTALL):
        html_chunk = text[last_end:match.start()]
        parts.append(repair_html_chunk(html_chunk))

        open_tag, body, close_tag = match.groups()
        if "src=" in open_tag.lower():
            parts.append(open_tag + body + close_tag)
        else:
            parts.append(open_tag + repair_js(body) + close_tag)

        last_end = match.end()

    parts.append(repair_html_chunk(text[last_end:]))
    repaired = "".join(parts)
    for bad in sorted(LITERAL_REPLACEMENTS, key=len, reverse=True):
        repaired = repaired.replace(bad, LITERAL_REPLACEMENTS[bad])
    return repaired


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: repair_mojibake.py <input> [output]", file=sys.stderr)
        return 1

    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src

    original = src.read_text(encoding="utf-8")
    repaired = repair_document(original)
    dst.write_text(repaired, encoding="utf-8", newline="")
    print(f"repaired {src} -> {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
