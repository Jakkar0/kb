#!/usr/bin/env python3
"""Generate a Keebio Iris (Vial) .vil layout from the Toucan2 ZMK keymap.

The Toucan2 lacks the Iris's extra number row, but rows/columns/thumb keys
otherwise line up. This tool merges your Toucan2 keymap into an Iris .vil
template:

- Rows 0 and 5 of each .vil layer (the Iris number row) are kept from the
  template. The extra innermost thumb keys (row 4/9, col 5), which have no
  physical equivalent on the Toucan2, are set to KC_NO (unused).
- ZMK conditional layers (e.g. NUNA+SYM -> MISC) are parsed and, by
  default, emulated via a double-MO trick because QMK tri-layer needs
  firmware support (see CONDITIONAL_LAYER in the config).
- Everything else on every layer is regenerated from the ZMK keymap.
- All other .vil sections (uid, macros, tap dance, combos, key overrides,
  settings, ...) are passed through unchanged.

Usage:
    python3 gen_iris_vil.py                # uses default paths below
    python3 gen_iris_vil.py --print-only    # report only, don't write

Default layout of this meta-repo:
    keebio-iris_se-rev8/gen_iris_vil.py    (this script)
    keebio-iris_se-rev8/iris_se.vil        (template: gaming top rows etc.)
    zmk-keyboard-toucan2/config/toucan.keymap   (sibling fork checkout)
"""

import argparse
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent        # keebio-iris_se-rev8/
ROOT = HERE.parent                            # meta-repo root
KEYMAP = ROOT / "zmk-keyboard-toucan2" / "config" / "toucan.keymap"
TEMPLATE = HERE / "iris_se.vil"
OUTPUT = HERE / "iris_se.generated.vil"

# ZMK layer label -> Iris (Vial) layer index.
# The Iris Vial firmware has 4 layers, so only 4 of the Toucan2's 5 layers fit.
# Layer choice matches the existing iris_se.vil: layer 1 is SYM (right thumb
# MO), layer 2 is NUNA (left thumb MO), layer 3 is MISC (tri-layer).
LAYER_MAP = {
    "base": 0,
    "sym": 1,
    "nuna": 2,
    "misc": 3,
}

# Toucan2 layers with no Iris slot. Their bindings are parsed but never
# emitted; anything that references them is an error (see MO handling below).
DROPPED_LAYERS = {"mouse"}  # trackpad-only layer, auto-activated by touch

# ZMK conditional layers (e.g. `if-layers = <NUNA SYM>; then-layer = <MISC>`)
# need firmware support on the Iris: QMK only behaves that way when built
# with `TRI_LAYER_ENABLE`, and the stock Keebio Vial build does not appear
# to include it. Strategies:
#   "double-mo": pure-keymap emulation. On layer B, the key that activates
#                layer A becomes MO(then-layer), and vice versa - pressing
#                the second MO key while the first is held activates the
#                conditional layer. Works with any firmware.
#   "firmware":  emit nothing into the keymap; requires a firmware build
#                with TRI_LAYER_ENABLE (lower/raise = the two MO layers,
#                adjust = the then-layer).
#   "ignore":    emit nothing.
CONDITIONAL_LAYER = "double-mo"

# Expected ZMK bindings per layer (3x12 main + 6 thumb keys = 42 keys).
BINDINGS_PER_LAYER = 42

# Position mapping from the Toucan2 matrix transform to binding order. The
# transform is 12 wide x 4: each keymap line is one full row (6 left-half
# keys + 6 right-half keys), followed by 6 thumb keys.
TOUCAN_LAYOUT = [
    *[(r, c) for r in range(3) for c in range(12)],  # main rows, 12 wide
    (3, 3), (3, 4), (3, 5),                          # left thumb outer->inner
    (3, 6), (3, 7), (3, 8),                          # right thumb inner->outer
]

# .vil rows owned by the Iris template: the top (number) rows have no
# Toucan2 equivalent and are kept as configured in the template.
IRIS_TEMPLATE_ROWS = {0: "top row (left)", 5: "top row (right)"}
IRIS_PRESERVED = {(r, c) for r in IRIS_TEMPLATE_ROWS for c in range(6)}
IRIS_DUMMY = {(4, 0), (4, 1), (9, 0), (9, 1)}  # always -1
# The Iris has one extra innermost thumb key per half with no Toucan2
# equivalent: forced to KC_NO (unused) on every layer.
IRIS_THUMB_NO = {(4, 5), (9, 5)}

# ---------------------------------------------------------------------------
# ZMK key code -> Vial/QMK keycode name translation
# Names match vial-gui's keycode database (verified against vial-gui v0.7.5
# and the existing iris_se.vil).
# ---------------------------------------------------------------------------

ZMK_TO_VIAL = {
    # letters
    **{chr(ord('A') + i): f"KC_{chr(ord('A') + i)}" for i in range(26)},
    # digits
    **{f"N{i}": f"KC_{i}" for i in range(10)},
    # typewriter
    "TAB": "KC_TAB", "ENTER": "KC_ENTER", "RET": "KC_ENTER", "SPACE": "KC_SPACE",
    "BSPC": "KC_BSPACE", "ESCAPE": "KC_ESCAPE", "ESC": "KC_ESCAPE",
    "LBKT": "KC_LBRACKET", "RBKT": "KC_RBRACKET", "SEMI": "KC_SCOLON",
    "SQT": "KC_QUOTE", "COMMA": "KC_COMMA", "DOT": "KC_DOT",
    "BSLH": "KC_BSLASH", "FSLH": "KC_SLASH", "MINUS": "KC_MINUS",
    "EQUAL": "KC_EQUAL", "GRAVE": "KC_GRAVE",
    "NUBS": "KC_NONUS_BSLASH", "NUHS": "KC_NONUS_HASH",
    "CAPS": "KC_CAPSLOCK", "CAPSLOCK": "KC_CAPSLOCK", "NUMLOCK": "KC_NUMLOCK",
    "SLCK": "KC_SCROLLLOCK", "PAUSE": "KC_PAUSE",
    # modifiers
    "LCTRL": "KC_LCTRL", "RCTRL": "KC_RCTRL",
    "LSHFT": "KC_LSHIFT", "RSHFT": "KC_RSHIFT",
    "LALT": "KC_LALT", "RALT": "KC_RALT",
    "LGUI": "KC_LGUI", "RGUI": "KC_RGUI",
    # navigation
    "INS": "KC_INSERT", "DEL": "KC_DELETE", "HOME": "KC_HOME", "END": "KC_END",
    "PG_UP": "KC_PGUP", "PG_DN": "KC_PGDOWN",
    "UP": "KC_UP", "DOWN": "KC_DOWN", "LEFT": "KC_LEFT", "RIGHT": "KC_RIGHT",
    # function keys
    **{f"F{i}": f"KC_F{i}" for i in range(1, 13)},
    "PSCRN": "KC_PSCREEN",
    # consumer/media
    "C_VOL_UP": "KC_VOLU", "C_VOL_DN": "KC_VOLD", "C_MUTE": "KC_MUTE",
    "C_PP": "KC_MPLY", "C_PREV": "KC_MPRV", "C_NEXT": "KC_MNXT",
    "C_STOP": "KC_MSTP", "C_EJCT": "KC_EJCT",
    # shifted (US layout) conveniences used by some ZMK keymaps
    "EXCL": "LSFT(KC_1)", "AT": "LSFT(KC_2)", "HASH": "LSFT(KC_3)",
    "DLLR": "LSFT(KC_4)", "PRCNT": "LSFT(KC_5)", "CARET": "LSFT(KC_6)",
    "AMPS": "LSFT(KC_7)", "STAR": "LSFT(KC_8)",
    "LPRN": "LSFT(KC_9)", "RPRN": "LSFT(KC_0)",
    "UNDER": "LSFT(KC_MINUS)", "PLUS": "LSFT(KC_EQUAL)",
    "LBRC": "LSFT(KC_LBRACKET)", "RBRC": "LSFT(KC_RBRACKET)",
    "PIPE": "LSFT(KC_BSLASH)", "COLON": "LSFT(KC_SCOLON)",
    "QMARK": "LSFT(KC_SLASH)", "TILDE": "LSFT(KC_GRAVE)",
}

# ZMK modifier prefixes -> Vial modifier masks
ZMK_MODS = {
    "LC": "LCTL", "LS": "LSFT", "LA": "LALT", "LG": "LGUI",
    "RC": "RCTL", "RS": "RSFT", "RA": "RALT", "RG": "RGUI",
}

# ZKP mouse button codes (Vial: MOUSEKEY support in firmware required to
# actually function)
ZMK_TO_VIAL_MKP = {
    "LCLK": "KC_BTN1", "MCLK": "KC_BTN3", "RCLK": "KC_BTN2",
}

# Multi-modifier combos that QMK/Vial encode as single compound masks.
# Left-mod combos only; right-side combinations have no QMK mask name.
MOD_COMPOUNDS = {
    frozenset(("LCTL", "LSFT")): "C_S",
    frozenset(("LCTL", "LALT")): "LCA",
    frozenset(("LCTL", "LGUI")): "LCAG",
    frozenset(("LSFT", "LGUI")): "SGUI",
    frozenset(("LCTL", "LSFT", "LALT")): "MEH",
    frozenset(("LCTL", "LSFT", "LALT", "LGUI")): "HYPR",
}


class TranslationError(Exception):
    pass


class Warn:
    """Collects non-fatal notes for the report."""

    def __init__(self):
        self.items = []

    def add(self, msg):
        self.items.append(msg)

    def dump(self):
        for item in self.items:
            print(f"  WARNING: {item}")


# ---------------------------------------------------------------------------
# ZMK keymap parsing
# ---------------------------------------------------------------------------

def parse_keymap(path):
    """Return (layers, defines) where layers is a list of dicts in
    declaration order: {"label", "bindings"} and bindings is a list of
    ("&behavior", [params]) tuples, or None for layers without bindings."""
    src = path.read_text()
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)

    defines = dict(re.findall(r"#\s*define\s+(\w+)\s+(\w+)", src))

    m = re.search(r"\bkeymap\s*\{", src)
    if not m:
        raise TranslationError(f"{path}: no `keymap {{ ... }}` node found")
    # Take the rest of the file; layer children contain no nested braces.
    body = src[m.end():]

    layers = []
    for label, inner in re.findall(r"(\w+)\s*\{([^{}]*)\}", body):
        b = re.search(r"bindings\s*=\s*<\s*(.*?)\s*>", inner, flags=re.S)
        if not b:
            if "reserved" in inner or "status" in inner:
                layers.append({"label": label, "bindings": None})
                continue
            raise TranslationError(f"layer `{label}` has neither bindings nor a status")
        tokens = b.group(1).split()
        bindings = []
        current = None
        for tok in tokens:
            if tok.startswith("&"):
                if current is not None:
                    bindings.append(current)
                current = (tok[1:], [])
            elif current is None:
                raise TranslationError(f"layer `{label}`: token {tok!r} before any behavior")
            else:
                current[1].append(tok)
        if current is not None:
            bindings.append(current)
        layers.append({"label": label, "bindings": bindings})
    if not layers:
        raise TranslationError(f"{path}: no layers found in keymap node")
    return layers, defines


def parse_conditional_layers(src, layers, defines):
    """Parse `conditional_layers` child nodes. Returns a list of
    (label, [zmk if-layer idx...], zmk then-layer idx)."""
    out = []
    m = re.search(r"\bconditional_layers\s*\{", src)
    if not m:
        return out
    end = re.search(r"^\s*/\s*\{", src[m.end():], flags=re.M)
    region = src[m.end():m.end() + end.start()] if end else src[m.end():]
    for label, body in re.findall(r"(\w+)\s*\{([^{}]*)\}", region):
        if_m = re.search(r"if-layers\s*=\s*<([^<>]*)>", body)
        then_m = re.search(r"then-layer\s*=\s*<?(\w+)", body)
        if not if_m or not then_m:
            continue  # properties of the outer node, or an unrelated block
        if_layers = [resolve_zmk_layer(p, layers, defines)
                     for p in if_m.group(1).split()]
        then_idx = resolve_zmk_layer(then_m.group(1), layers, defines)
        out.append((label, if_layers, then_idx))
    return out


# ---------------------------------------------------------------------------
# Binding translation
# ---------------------------------------------------------------------------

def kp_to_vial(code, where, warn):
    """Translate one ZMK `&kp` argument to a Vial keycode string."""
    m = re.match(r"^(\w+)\((.+)\)$", code)
    if m:
        mods = [m.group(1)]
        inner = m.group(2)
        # unwrap nested modifiers, e.g. LC(LG(UP))
        while True:
            n = re.match(r"^(\w+)\((.+)\)$", inner)
            if n and n.group(1) in ZMK_MODS:
                mods.append(n.group(1))
                inner = n.group(2)
            else:
                break
        try:
            masks = [ZMK_MODS[mod] for mod in mods]
        except KeyError:
            raise TranslationError(f"{where}: unknown modifier(s) {mods} in `{code}`")
        if len(masks) == 1:
            return f"{masks[0]}({kp_to_vial(inner, where, warn)})"
        compound = MOD_COMPOUNDS.get(frozenset(masks))
        if compound:
            return f"{compound}({kp_to_vial(inner, where, warn)})"
        raise TranslationError(
            f"{where}: modifier combination {'+'.join(sorted(masks))} in `{code}` "
            f"has no single QMK/Vial mask; add it to MOD_COMPOUNDS if one exists")
    leaf = ZMK_TO_VIAL.get(code)
    if leaf is None:
        raise TranslationError(f"{where}: ZMK code `{code}` not in ZMK_TO_VIAL")
    return leaf


def resolve_zmk_layer(param, layers, defines):
    """`&mo` parameter (number or name via #define/label) -> ZMK layer index."""
    if param in defines:
        param = defines[param]
    if param.isdigit():
        idx = int(param)
        if idx >= len(layers):
            raise TranslationError(f"layer index {idx} out of range")
        return idx
    for i, layer in enumerate(layers):
        if layer["label"] == param:
            return i
    raise TranslationError(f"unknown layer reference `{param}`")


def translate_binding(binding, where, layers, define, warn):
    """Return the Vial keycode string for one binding tuple, or None when
    the binding has no Vial equivalent (warned instead of failing)."""
    behavior, params = binding
    if behavior == "trans":
        return "KC_TRNS"
    if behavior == "none":
        return "KC_NO"
    if behavior == "mo" or behavior == "momentary":
        zmk_idx = resolve_zmk_layer(params[0], layers, define)
        label = layers[zmk_idx]["label"]
        if label in LAYER_MAP:
            return f"MO({LAYER_MAP[label]})"
        if label in DROPPED_LAYERS:
            warn.add(f"{where}: `&mo {params[0]}` targets dropped layer `{label}` -> KC_NO")
            return "KC_NO"
        raise TranslationError(f"{where}: `&mo {params[0]}` -> layer `{label}` has no LAYER_MAP/DROPPED_LAYERS entry")
    if behavior in ("to", "tog"):
        zmk_idx = resolve_zmk_layer(params[0], layers, define)
        label = layers[zmk_idx]["label"]
        if label in LAYER_MAP:
            fn = "TO" if behavior == "to" else "TG"
            return f"{fn}({LAYER_MAP[label]})"
        if label in DROPPED_LAYERS:
            warn.add(f"{where}: `&{behavior} {params[0]}` targets dropped layer `{label}` -> KC_NO")
            return "KC_NO"
        raise TranslationError(f"{where}: layer switch to layer `{label}` has no LAYER_MAP entry")
    if behavior == "kp":
        if len(params) != 1:
            raise TranslationError(f"{where}: `&kp` expects 1 param, got {params}")
        return kp_to_vial(params[0], where, warn)
    if behavior == "mkp":
        if len(params) != 1:
            raise TranslationError(f"{where}: `&mkp` expects 1 param, got {params}")
        code = ZMK_TO_VIAL_MKP.get(params[0])
        if code is None:
            raise TranslationError(f"{where}: unknown mouse code `{params[0]}`")
        return code
    if behavior == "bt":
        warn.add(f"{where}: `&bt {' '.join(params)}` has no Vial equivalent on a wired keyboard -> KC_NO")
        return "KC_NO"
    if behavior == "out":
        warn.add(f"{where}: `&out {' '.join(params)}` has no Vial equivalent -> KC_NO")
        return "KC_NO"
    if behavior == "studio_unlock":
        warn.add(f"{where}: `&studio_unlock` has no Vial equivalent -> KC_NO")
        return "KC_NO"
    if behavior in ("sys_reset", "reset", "bootloader"):
        warn.add(f"{where}: `&{behavior}` has no Vial equivalent -> KC_NO")
        return "KC_NO"
    raise TranslationError(f"{where}: unhandled behavior `&{behavior} {' '.join(params)}`")


# ---------------------------------------------------------------------------
# Merge into the Iris .vil template
# ---------------------------------------------------------------------------

def apply_conditional_layers(conds, layers, new_layout, warn):
    """Rewrite MO keys so holding both if-layer keys activates the
    then-layer, emulating ZMK conditional layers without firmware support."""
    notes = []
    if CONDITIONAL_LAYER not in ("double-mo", "firmware", "ignore"):
        raise TranslationError(f"invalid CONDITIONAL_LAYER {CONDITIONAL_LAYER!r}")
    if CONDITIONAL_LAYER != "double-mo":
        return notes
    num = len(new_layout)
    for label, if_layers, then_idx in conds:
        if len(if_layers) != 2:
            warn.add(f"conditional `{label}`: only two if-layers supported "
                     f"(got {len(if_layers)}); skipped")
            continue
        names = [layers[i]["label"] for i in if_layers]
        if any(n not in LAYER_MAP for n in names + [layers[then_idx]["label"]]):
            warn.add(f"conditional `{label}`: references a layer without an "
                     f"Iris mapping ({names + [layers[then_idx]['label']]}); skipped")
            continue
        a = LAYER_MAP[names[0]]
        b = LAYER_MAP[names[1]]
        then = LAYER_MAP[layers[then_idx]["label"]]
        if not (0 <= a < num and 0 <= b < num and 0 <= then < num):
            raise TranslationError(f"conditional `{label}`: layer index out of range")
        pos = {}
        for idx in (a, b):
            code = f"MO({idx})"
            found = [(r, c) for grid in new_layout for r in range(10)
                     for c in range(6) if grid[r][c] == code]
            if not found:
                warn.add(f"conditional `{label}`: no {code} keys in the "
                         f"generated layout to build on; skipped")
                break
            pos[idx] = found
        if pos.get(a) is None or pos.get(b) is None:
            continue
        # On layer B, the key that would activate layer A jumps straight to
        # the then-layer (and the other way round).
        for target, source in ((b, a), (a, b)):
            for r, c in pos[source]:
                if (r, c) in IRIS_PRESERVED or (r, c) in IRIS_DUMMY:
                    raise TranslationError(
                        f"conditional `{label}`: MO position ({r},{c}) is a "
                        f"template/dummy cell and cannot be rewritten")
                before = new_layout[target][r][c]
                new_layout[target][r][c] = f"MO({then})"
                notes.append(
                    f"conditional `{label}`: layer {target} key at .vil[{r}][{c}] "
                    f"{before} -> MO({then})")
    return notes


def toucan_binding_index(layer_bindings, rc):
    """Map RC(row, col) -> binding index for the 42-key Toucan2 layout."""
    return TOUCAN_LAYOUT.index(rc)


def build_iris_layers(layers, defines, conds, template, warn):
    num_iris_layers = len(template["layout"])
    iris_layers = []
    vial_names_used = set()

    for zmk_idx, layer in enumerate(layers):
        label = layer["label"]
        if layer["bindings"] is None:
            continue  # reserved placeholder; occupies a ZMK index but has no keys
        if label in DROPPED_LAYERS:
            continue
        if len(layer["bindings"]) != BINDINGS_PER_LAYER:
            raise TranslationError(
                f"layer `{label}`: found {len(layer['bindings'])} bindings, "
                f"expected {BINDINGS_PER_LAYER} (keymap layout changed?)")
        if label not in LAYER_MAP:
            raise TranslationError(
                f"layer `{label}`: no entry in LAYER_MAP or DROPPED_LAYERS; "
                f"edit the config at the top of this script")
        iris_idx = LAYER_MAP[label]
        if iris_idx >= num_iris_layers:
            raise TranslationError(
                f"layer `{label}` -> Iris layer {iris_idx}, but the template only has "
                f"{num_iris_layers} layers")
        if any(l["zmk_label"] == label for l in iris_layers):
            raise TranslationError(f"layer `{label}` mapped to multiple Iris layers")

        grid = [row[:] for row in template["layout"][iris_idx]]
        # regenerate every Toucan-derivable position
        assignments = []
        for r in range(3):
            for c in range(6):  # left
                assignments.append(((r + 1, c), (r, c)))
        for r in range(3):
            for c in range(6):  # right; .vil right rows are stored reversed
                assignments.append(((r + 6, c), (r, 11 - c)))
        assignments.append(((4, 2), (3, 3)))
        assignments.append(((4, 3), (3, 4)))
        assignments.append(((4, 4), (3, 5)))
        assignments.append(((9, 2), (3, 8)))   # right thumb outermost
        assignments.append(((9, 3), (3, 7)))
        assignments.append(((9, 4), (3, 6)))   # right thumb innermost

        for (iris_r, iris_c), rc in assignments:
            if (iris_r, iris_c) in IRIS_PRESERVED or (iris_r, iris_c) in IRIS_DUMMY:
                raise TranslationError(
                    f"internal error: Toucan position {rc} collides with preserved "
                    f"position ({iris_r},{iris_c})")
            binding = layer["bindings"][toucan_binding_index(layer["bindings"], rc)]
            where = f"[{label} @ {rc[0]},{rc[1]} -> vil {iris_r},{iris_c}]"
            kc = translate_binding(binding, where, layers, defines, warn)
            if kc is not None:
                grid[iris_r][iris_c] = kc
                vial_names_used.add(kc)

        iris_layers.append({"zmk_label": label, "iris_idx": iris_idx, "grid": grid})

    # sanity: every mapped Iris layer filled exactly once
    seen = [l["iris_idx"] for l in iris_layers]
    if len(seen) != len(set(seen)):
        raise TranslationError("multiple ZMK layers mapped to the same Iris layer")

    new_layout = [None] * num_iris_layers
    for l in iris_layers:
        new_layout[l["iris_idx"]] = l["grid"]
    for i in range(num_iris_layers):
        if new_layout[i] is None:
            # No Toucan layer mapped here: keep the template as-is.
            new_layout[i] = template["layout"][i]

    cond_notes = apply_conditional_layers(conds, layers, new_layout, warn)

    # sanity checks on dummies, template rows and forced thumb keys
    for li, grid in enumerate(new_layout):
        for (r, c) in IRIS_DUMMY:
            if grid[r][c] != -1:
                raise TranslationError(f"layer {li} dummy cell ({r},{c}) is {grid[r][c]!r}")
        for (r, c) in IRIS_PRESERVED:
            if grid[r][c] != template["layout"][li][r][c]:
                raise TranslationError(f"layer {li} preserved cell ({r},{c}) was modified")
        for (r, c) in IRIS_THUMB_NO:
            grid[r][c] = "KC_NO"

    return new_layout, vial_names_used, cond_notes


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

IRIS_ROW_NAMES = {0: "top", 1: "tab", 2: "home", 3: "bot", 4: "thumb"}


def print_keymap(title, grid):
    print(f"\n  {title}")
    for half, offset in (("LEFT", 0), ("RIGHT", 5)):
        print(f"    {half} half (outermost column first, as stored in .vil):")
        for r in range(offset, offset + 5):
            cells = " ".join(f"{str(kc):22}" for kc in grid[r][:4])
            cells2 = " ".join(f"{str(kc):22}" for kc in grid[r][4:])
            mark = "*" if r in IRIS_TEMPLATE_ROWS else ""
            print(f"      row {r}{mark}: {cells}{cells2}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keymap", type=Path, default=KEYMAP)
    ap.add_argument("--template", type=Path, default=TEMPLATE)
    ap.add_argument("--out", type=Path, default=OUTPUT)
    ap.add_argument("--print-only", action="store_true",
                    help="do not write the output file")
    args = ap.parse_args()

    warn = Warn()
    for p, what in ((args.keymap, "ZMK keymap"), (args.template, ".vil template")):
        if not p.exists():
            print(f"ERROR: {what} not found at {p}\n"
                  f"(pass --keymap/--template, or check the meta-repo layout)",
                  file=sys.stderr)
            return 1
    try:
        layers, defines = parse_keymap(args.keymap)
        template = json.loads(args.template.read_text())
        conds = parse_conditional_layers(args.keymap.read_text(), layers, defines)
        new_layout, vial_names_used, cond_notes = build_iris_layers(
            layers, defines, conds, template, warn)
    except (TranslationError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        warn.dump()
        return 1

    num_template_layers = len(template["layout"])
    print(f"Parsed {args.keymap.name}:")
    for i, layer in enumerate(layers):
        n = "no bindings (reserved)" if layer["bindings"] is None else f"{len(layer['bindings'])} keys"
        target = LAYER_MAP.get(layer["label"], "(dropped)" if layer["label"] in DROPPED_LAYERS else "(no mapping)")
        print(f"  ZMK layer {i} `{layer['label']}`: {n} -> Iris layer {target}")

    changed = 0
    for li in range(num_template_layers):
        for r in range(10):
            for c in range(6):
                if template["layout"][li][r][c] != new_layout[li][r][c]:
                    changed += 1
    print(f"\nIris .vil layers: {num_template_layers}; keys changed vs template: {changed}")

    if warn.items:
        print("\nWarnings (keys without a Vial equivalent were set to KC_NO):")
        warn.dump()

    if cond_notes:
        print("\nConditional layers (ZMK tri-layer) emulated via double-MO keys:")
        for note in cond_notes:
            print("  " + note)

    for li, grid in enumerate(new_layout):
        print_keymap(f"Iris layer {li}", grid)

    print('\n  * = row kept from template (Iris number row)')
    print("  The extra innermost thumb keys (rows 4/9, col 5) are set to KC_NO on every layer.")

    # Collect all emitted keycode names that are not already present verbatim
    # anywhere in the template file - these are new to your Vial install.
    template_text = args.template.read_text()
    new_names = sorted({
        kc for kc in vial_names_used
        if not re.match(r"^(KC_TRNS|KC_NO|-?\d+)$", kc)
        and F'"{kc}"' not in template_text
        and kc not in ("KC_TRNS", "KC_NO")
    })
    if new_names:
        print("\nKeycodes introduced that were not in your template .vil")
        print("(must exist in Vial's keycode DB for import to work):")
        print("  " + ", ".join(new_names))

    if args.print_only:
        return 0

    out = dict(template)
    out["layout"] = new_layout
    args.out.write_text(json.dumps(out))
    rel = args.out.relative_to(args.template.parent) if args.out.is_relative_to(
        args.template.parent) else args.out
    print(f"\nWrote {rel}")
    print("Import it in Vial: File -> Import Saved Layout (Vial will accept it as the")
    print("same keyboard since the uid was preserved).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
