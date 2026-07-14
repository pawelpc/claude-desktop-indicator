"""Visual encoding definitions for the Claude Desktop indicator.

Single source of truth for the color/shape scheme defined in
``_context/design-spec.md``. Do not change values here without approval —
the encoding is fixed by design (drift rule R4).

Encoding summary:
    background color -> mode (cool hues)
    center shape + fill color -> model family (warm hues)
    text inside shape -> model version
"""

# Mode -> window background color (cool side of the color wheel).
MODE_COLORS = {
    "chat": "#1E40AF",    # deep blue,  ~220 deg
    "cowork": "#7C3AED",  # violet,     ~265 deg
    "code": "#0E7490",    # teal,       ~195 deg
}

# Background used when the mode could not be determined.
UNKNOWN_MODE_COLOR = "#374151"  # neutral slate

# Background used while waiting for Claude Desktop to appear.
WAITING_COLOR = "#1F2937"  # dark slate

# Model family -> (shape, fill color). Warm hues, never colliding with the
# cool mode backgrounds.
FAMILY_SHAPES = {
    "opus": ("circle", "#DC2626"),     # red,    ~0 deg
    "sonnet": ("diamond", "#EA580C"),  # orange, ~25 deg
    "haiku": ("triangle", "#16A34A"),  # green,  ~140 deg
    "fable": ("pentagon", "#CA8A04"),  # gold,   ~45 deg
}

# Shape/fill used when the model family could not be determined.
UNKNOWN_FAMILY_SHAPE = ("square", "#6B7280")  # neutral gray square + '?'

# Text color for version digits and the summary label.
TEXT_COLOR = "#FFFFFF"
