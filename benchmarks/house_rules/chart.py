"""Render the learning curve from results.json as light and dark SVGs for the README (<picture> switches by theme).

    uv run python benchmarks/house_rules/chart.py
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent
FAMILIES = [("commit", "Commit messages"), ("migration", "SQL migrations"), ("support", "Support replies"),
            ("python", "Python functions")]
THEMES = {  # validated categorical slots 1-4 (light / dark steps) + surfaces and ink
    "light": {"surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#8a897f", "grid": "#e6e5df",
              "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]},
    "dark": {"surface": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#8f8e86", "grid": "#33322f",
             "series": ["#3987e5", "#d95926", "#199e70", "#c98500"]},
}
W, H = 820, 430
L, R, T, B = 64, 190, 78, 54  # plot margins (right side holds direct labels)


def svg(results: dict, theme: str) -> str:
    th = THEMES[theme]
    curve = results["curve"]
    xs = [c["train_runs"] for c in curve]
    xmax = max(xs) or 1
    pw, ph = W - L - R, H - T - B

    def X(v: float) -> float:
        return L + pw * v / xmax

    def Y(v: float) -> float:
        return T + ph * (1 - v)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
           f'font-family="-apple-system, Segoe UI, Helvetica, Arial, sans-serif" role="img" '
           f'aria-label="Held-out pass rate rises from {curve[0]["overall"]:.0%} to {curve[-1]["overall"]:.0%} '
           f'over {xmax} training runs">',
           f'<rect width="{W}" height="{H}" rx="12" fill="{th["surface"]}"/>',
           f'<text x="{L}" y="32" font-size="17" font-weight="600" fill="{th["ink"]}">Held-out pass rate while '
           f'Myelinate learns team conventions</text>',
           f'<text x="{L}" y="54" font-size="12.5" fill="{th["ink2"]}">{results["test_tasks"]} unseen tasks × '
           f'{results["samples_per_test_task"]} samples · agent {results["agent_model"]} · rules never in the prompt'
           f'</text>']
    for v in (0, 0.25, 0.5, 0.75, 1.0):  # recessive grid + y axis
        out.append(f'<line x1="{L}" x2="{L + pw}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="{th["grid"]}" '
                   f'stroke-width="1"/>')
        out.append(f'<text x="{L - 10}" y="{Y(v) + 4:.1f}" font-size="11.5" text-anchor="end" '
                   f'fill="{th["muted"]}">{v:.0%}</text>')
    for x in xs:
        out.append(f'<text x="{X(x):.1f}" y="{T + ph + 20}" font-size="11.5" text-anchor="middle" '
                   f'fill="{th["muted"]}">{x}</text>')
    out.append(f'<text x="{L + pw / 2:.1f}" y="{H - 12}" font-size="12" text-anchor="middle" '
               f'fill="{th["ink2"]}">training runs recorded (with verifier feedback)</text>')

    series = [(name, key, th["series"][i], 2.0, 4) for i, (key, name) in enumerate(FAMILIES)]
    series.append(("Overall", "overall", th["ink"], 3.0, 5))
    labels = []
    for name, key, color, width, r in series:
        pts = [(X(c["train_runs"]), Y(c[key])) for c in curve]
        d = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(pts))
        out.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}" stroke-linejoin="round" '
                   f'stroke-linecap="round"/>')
        for x, y in pts:  # markers with a surface ring so overlapping points stay readable
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{color}" stroke="{th["surface"]}" '
                       f'stroke-width="2"/>')
        labels.append([pts[-1][1], name, curve[-1][key], color, key == "overall"])
    labels.sort(key=lambda lab: lab[0])  # direct labels at the line ends, spread so they never collide
    for i in range(1, len(labels)):
        labels[i][0] = max(labels[i][0], labels[i - 1][0] + 17)
    for y, name, val, color, bold in labels:
        out.append(f'<circle cx="{L + pw + 16}" cy="{y - 4:.1f}" r="4" fill="{color}"/>')
        out.append(f'<text x="{L + pw + 26}" y="{y:.1f}" font-size="12.5" fill="{th["ink"]}" '
                   f'font-weight="{600 if bold else 400}">{name} {val:.0%}</text>')
    out.append("</svg>")
    return "\n".join(out)


def main() -> None:
    results = json.loads((HERE / "results.json").read_text())
    assets = HERE.parents[1] / "docs" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    for theme in THEMES:
        (assets / f"benchmark-{theme}.svg").write_text(svg(results, theme))
    print("wrote", [f"docs/assets/benchmark-{t}.svg" for t in THEMES])


if __name__ == "__main__":
    main()
