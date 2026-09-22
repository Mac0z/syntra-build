from __future__ import annotations

import pytest

from syntra_build.adapters.telegram.design_approval import markdown_pdf

pytest.importorskip("fpdf")


@pytest.mark.parametrize(
    "markdown",
    (
        "# " + "A very long generated specification heading " * 20,
        "# " + "a" * 2_000,
        """# Realistic SPEC

## Storage and recovery guarantees for a deeply nested workflow

- Download https://example.invalid/a/very/long/path?with=query&and=parameters
- Verify sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
- Execute the documented CLI command and retain its complete configuration path.

```text
/var/lib/syntra-build/projects/project-with-a-very-long-name/worktrees/milestone
UNBROKEN_TOKEN_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789
```

Unicode review text: snowman ☃, emoji U0001f680, curly “quotes”.
"""
        + "`python -m syntra_build.smoke telegram-once --configuration="
        + "/etc/syntra-build/a/very/long/path.json`",
    ),
)
def test_markdown_pdf_wraps_realistic_long_content(markdown: str) -> None:
    rendered = markdown_pdf("Long Project", "SPEC", 17, markdown)
    assert rendered.startswith(b"%PDF")
    assert len(rendered) > 100
