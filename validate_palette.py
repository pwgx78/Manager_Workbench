"""validate_palette.py — check a chart palette is actually colourblind-safe.

A development utility, not part of the running app. Run it directly:

    python validate_palette.py

Kept in the repo rather than discarded because the mail-volume chart's planned
grouping (by subject, by sender) will add categorical series, and every added
series needs re-validating against the ones beside it. The alternative is
eyeballing whether two colours "look different enough", which is exactly the
mistake this prevents.

Checks, in the order reported: OKLCH lightness band, chroma floor (does it read
grey?), CVD separation under simulated protanopia/deuteranopia, a normal-vision
separation floor, and WCAG contrast against the chart surface.

The maths is transcribed from the reference JavaScript implementation in the
dataviz skill (Machado CVD matrices, sRGB -> linear -> OKLab, Euclidean OKLab
Delta E x100). The self-check at the bottom reproduces two figures the reference
palette documents for its full eight-slot run, which is what demonstrates the
port is faithful rather than merely plausible.
"""
import math

BAND = {"light": (0.43, 0.77), "dark": (0.48, 0.67)}  # OKLCH L
CHROMA_FLOOR = 0.10
CVD_TARGET, CVD_FLOOR = 8.0, 6.0
NORMAL_FLOOR = 15.0
CONTRAST_MIN = 3.0
DEFAULT_SURFACE = {"light": "#fcfcfb", "dark": "#1a1a19"}

MACHADO = {
    "protan": [[0.152286, 1.052583, -0.204868],
               [0.114503, 0.786281, 0.099216],
               [-0.003882, -0.048116, 1.051998]],
    "deutan": [[0.367322, 0.860646, -0.227968],
               [0.280085, 0.672501, 0.047413],
               [-0.011820, 0.042940, 0.968881]],
    "tritan": [[1.255528, -0.076749, -0.178779],
               [-0.078411, 0.930809, 0.147602],
               [0.004733, 0.691367, 0.303900]],
}


def hex2srgb(h):
    h = h.strip().lstrip("#")
    return [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]


def s2lin(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def lin(h):
    return [s2lin(c) for c in hex2srgb(h)]


def rel_lum(h):
    r, g, b = lin(h)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    hi, lo = sorted([rel_lum(a), rel_lum(b)], reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def oklab_from_lin(rgb):
    r, g, b = rgb
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return [
        0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
        1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
        0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s,
    ]


def oklch(h):
    L, a, b = oklab_from_lin(lin(h))
    return L, math.hypot(a, b)


def simulate(h, kind):
    r, g, b = lin(h)
    M = MACHADO[kind]
    return [
        min(1, max(0, M[i][0] * r + M[i][1] * g + M[i][2] * b)) for i in range(3)
    ]


def delta_e(h1, h2, kind=None):
    a = oklab_from_lin(simulate(h1, kind) if kind else lin(h1))
    b = oklab_from_lin(simulate(h2, kind) if kind else lin(h2))
    return 100 * math.dist(a, b)


def validate(palette, mode="light", surface=None, pairs="adjacent"):
    surface = surface or DEFAULT_SURFACE[mode]
    lo, hi = BAND[mode]
    ok = True
    print(f"\n=== {mode.upper()} · surface {surface} · {pairs} pairs ===")
    print("   palette:", ", ".join(palette))

    offband = [(c, round(oklch(c)[0], 3)) for c in palette
               if not lo <= oklch(c)[0] <= hi]
    ok &= not offband
    print(f"   {'PASS' if not offband else 'FAIL'}  Lightness band  "
          + (f"outside L {lo}-{hi}: {offband}" if offband
             else f"all {len(palette)} inside L {lo}-{hi}"))

    lowc = [(c, round(oklch(c)[1], 3)) for c in palette if oklch(c)[1] < CHROMA_FLOOR]
    ok &= not lowc
    print(f"   {'PASS' if not lowc else 'FAIL'}  Chroma floor    "
          + (f"below floor: {lowc}" if lowc else f"all {len(palette)} >= {CHROMA_FLOOR}"))

    n = len(palette)
    pairlist = ([(i, j) for i in range(n) for j in range(i + 1, n)]
                if pairs == "all" else [(i, i + 1) for i in range(n - 1)])
    worst = None
    for kind in ("protan", "deutan"):
        for i, j in pairlist:
            d = delta_e(palette[i], palette[j], kind)
            if worst is None or d < worst[0]:
                worst = (d, kind, palette[i], palette[j])
    tri = min((delta_e(palette[i], palette[j], "tritan") for i, j in pairlist), default=99)
    wd = worst[0] if worst else 99
    state = "PASS" if wd >= CVD_TARGET else ("FLOOR" if wd >= CVD_FLOOR else "FAIL")
    ok &= state != "FAIL"
    print(f"   {state}  CVD separation  worst {worst[3]}<->{worst[2]} dE {wd:.1f} "
          f"({worst[1]}) · tritan {tri:.1f}")

    nworst = min(((delta_e(palette[i], palette[j]), palette[i], palette[j])
                  for i, j in pairlist), default=(99, "", ""))
    nstate = "PASS" if nworst[0] >= NORMAL_FLOOR else "FAIL"
    ok &= nstate == "PASS"
    print(f"   {nstate}  Normal-vision   worst {nworst[2]}<->{nworst[1]} "
          f"dE {nworst[0]:.1f} (floor {NORMAL_FLOOR:.0f})")

    low = [(c, round(contrast(c, surface), 2)) for c in palette
           if contrast(c, surface) < CONTRAST_MIN]
    print(f"   {'PASS' if not low else 'RELIEF'}  Contrast        "
          + (f"below {CONTRAST_MIN}:1, relief required: {low}" if low
             else f"all {len(palette)} >= {CONTRAST_MIN}:1"))
    return ok


if __name__ == "__main__":
    # Self-check: reproduce a documented figure from references/palette.md. The
    # full eight-slot light run is stated as worst adjacent CVD dE 9.1 and
    # worst adjacent normal-vision dE 19.6 — if the port is faithful, those
    # numbers come out of this code.
    eight_light = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                   "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
    print("--- PORT SELF-CHECK (expect adjacent CVD 9.1, normal 19.6) ---")
    validate(eight_light, mode="light")

    # Received / Sent = categorical slots 1 and 2. Two series, so all-pairs and
    # adjacent are the same single pair; run all-pairs as the stricter label.
    print("\n--- THE CHART'S PALETTE ---")
    a = validate(["#2a78d6", "#eb6834"], mode="light", pairs="all")
    b = validate(["#3987e5", "#d95926"], mode="dark", pairs="all")
    print(f"\nlight ok={a}  dark ok={b}")
