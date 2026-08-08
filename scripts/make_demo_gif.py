"""Generate the README terminal demo GIF (requires Pillow)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "assets" / "envcause-demo.gif"
WIDTH, HEIGHT = 1100, 620
BACKGROUND = "#08111b"
PANEL = "#101c2a"
MUTED = "#7890a8"
WHITE = "#edf3f8"
AMBER = "#ffad32"
TEAL = "#48c8c8"


def font(size: int, *, bold: bool = False):
    candidates = [
        Path("/System/Library/Fonts/Monaco.ttf"),
        Path("/System/Library/Fonts/Supplemental/Menlo.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


BODY = font(23)
BOLD = font(25, bold=True)
SMALL = font(18)


def frame(lines: list[tuple[str, str]], *, cursor: bool = False) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((28, 28, WIDTH - 28, HEIGHT - 28), radius=18, fill=PANEL, outline="#26394d", width=2)
    for index, color in enumerate(("#ff625a", "#ffbd2e", "#28c840")):
        x = 58 + index * 28
        draw.ellipse((x, 54, x + 14, 68), fill=color)
    draw.text((WIDTH - 235, 48), "ENVCAUSE DEMO", font=SMALL, fill=MUTED)

    y = 100
    for text, color in lines:
        selected_font = BOLD if color in {AMBER, TEAL} else BODY
        draw.text((62, y), text, font=selected_font, fill=color)
        y += 38
    if cursor:
        draw.rectangle((63, y + 2, 77, y + 29), fill=TEAL)
    return image


def main() -> None:
    sequence = [
        ([('$ envcause --good good.env --bad bad.env -- python app.py', WHITE)], 1200),
        ([
            ('$ envcause --good good.env --bad bad.env -- python app.py', WHITE),
            ('Comparing known-good and known-bad configurations...', MUTED),
        ], 900),
        ([
            ('$ envcause --good good.env --bad bad.env -- python app.py', WHITE),
            ('Comparing known-good and known-bad configurations...', MUTED),
            ('8 settings changed. Testing combinations...', WHITE),
            ('[candidate 1] 4 changed variables', MUTED),
            ('[candidate 2] 2 changed variables', MUTED),
            ('[candidate 3] 1 changed variable', MUTED),
        ], 1500),
        ([
            ('$ envcause --good good.env --bad bad.env -- python app.py', WHITE),
            ('', WHITE),
            ('1-minimal failure-inducing change set:', AMBER),
            ('', WHITE),
            ('  FEATURE_NEW_AUTH: false  ->  true', TEAL),
            ('  JWT_ALGORITHM:   HS256  ->  RS256', TEAL),
            ('', WHITE),
            ('Found 2 culprit settings out of 8 changes.', WHITE),
        ], 3200),
    ]
    frames = [frame(lines, cursor=index < len(sequence) - 1) for index, (lines, _) in enumerate(sequence)]
    durations = [duration for _, duration in sequence]
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        OUTPUT,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
    )
    print(OUTPUT)


if __name__ == "__main__":
    main()
