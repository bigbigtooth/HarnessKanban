#!/usr/bin/env python3
"""Extract the demo stylesheets into one Vue-consumable CSS file.

- demo-light.html carries the light/base stylesheet.
- demo-dark.html reuses the same base and appends a "COSMIC THEME OVERRIDES" block.
  Those overrides are re-scoped to ``html[data-theme="dark"]`` so a single document can
  switch themes by toggling the attribute on <html>.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path("/Users/davidwu/GithubProject/HarnessTask")
LIGHT = ROOT / "docs/html/demo-light.html"
DARK = ROOT / "docs/html/demo-dark.html"
OUT = ROOT / "frontend/src/styles/demo.css"


def style_block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index("<style>") + len("<style>")
    end = text.index("</style>", start)
    return text[start:end]


def split_top_level(css: str) -> list[str]:
    """Split a stylesheet into top-level rules / at-rule blocks (bodies kept intact)."""
    rules: list[str] = []
    depth = 0
    current: list[str] = []
    for char in css:
        current.append(char)
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                rules.append("".join(current).strip())
                current = []
    tail = "".join(current).strip()
    if tail:
        rules.append(tail)
    return [rule for rule in rules if rule]


AT_BLOCK_RE = re.compile(r"^(@[\w-]+)\s*([^{]*)\{(.*)\}$", re.S)
PLAIN_RULE_RE = re.compile(r"^(.*?)\{(.*)\}$", re.S)


def scope_rule(rule: str, prefix: str) -> str:
    at = AT_BLOCK_RE.match(rule)
    if at:
        name, prelude, body = at.group(1), at.group(2).strip(), at.group(3)
        if name in ("@media", "@supports", "@container", "@layer"):
            inner = "\n".join(scope_rule(part, prefix) for part in split_top_level(body))
            return f"{name} {prelude} {{\n{inner}\n}}"
        # @keyframes / @font-face / @property keep their bodies untouched.
        return rule

    plain = PLAIN_RULE_RE.match(rule)
    if not plain:
        return rule
    selectors, body = plain.group(1).strip(), plain.group(2)
    scoped = []
    for selector in selectors.split(","):
        selector = selector.strip()
        if not selector:
            continue
        if selector in (":root", "html"):
            scoped.append(prefix)
        else:
            scoped.append(f"{prefix} {selector}")
    return ",\n".join(scoped) + " {" + body + "}"


LIGHT_HEADER = """/* =========================================================
   HarnessTask 视觉样式（源自 docs/html/demo-light.html / demo-dark.html）

   - 未加前缀的规则 = demo-light 的浅色主题（默认主题）。
   - `html[data-theme="dark"] ...` 规则 = demo-dark 的深色覆盖层。

   该文件由 scripts/extract_demo_css.py 生成；如需调整请改脚本或在其后追加 app.css。
   ========================================================= */
"""


def main() -> None:
    base = style_block(LIGHT)
    dark_full = style_block(DARK)
    # Reuse the identical base from the light file; take only the appended override block.
    marker = dark_full.index("★ COSMIC THEME OVERRIDES ★")
    comment_end = dark_full.index("*/", marker) + len("*/")
    dark_block = dark_full[comment_end:]

    dark_block = re.sub(r"/\*.*?\*/", "", dark_block, flags=re.S)
    scoped_rules = []
    for rule in split_top_level(dark_block):
        if rule.lstrip().startswith("/*"):
            continue
        scoped_rules.append(scope_rule(rule, 'html[data-theme="dark"]'))
    scoped = "\n\n".join(scoped_rules)
    header = (
        "\n/* =========================================================\n"
        "   ★ DARK THEME OVERRIDES (from demo-dark.html) ★\n"
        "   Every selector is scoped to html[data-theme=\"dark\"].\n"
        "   ========================================================= */\n"
    )
    OUT.write_text(LIGHT_HEADER + base.rstrip() + "\n" + header + scoped.rstrip() + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    print("top-level light rules:", len(split_top_level(base)))
    print("top-level dark rules:", len(split_top_level(dark_block)))


if __name__ == "__main__":
    main()
