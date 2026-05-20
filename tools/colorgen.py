#!/usr/bin/env python3
"""
Color Table Generator with Perceptual Normalization

Generates HSL color tables with optional normalization to uniform perceptual
intensity (CIELAB L*) followed by global lightness scaling.
"""

import argparse
import colorsys
import math
import sys


def hsl_to_rgb(h, s, l):
    """Convert HSL (0-360, 0-100, 0-100) to RGB (0.0-1.0)."""
    h_norm = (h % 360) / 360.0
    s_norm = max(0, min(100, s)) / 100.0
    l_norm = max(0, min(100, l)) / 100.0
    r, g, b = colorsys.hls_to_rgb(h_norm, l_norm, s_norm)
    return (r, g, b)


def rgb_to_hex(r, g, b):
    """Convert RGB 0.0-1.0 to lowercase hex string."""
    r = max(0, min(1, r))
    g = max(0, min(1, g))
    b = max(0, min(1, b))
    return f"{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"


# CIELAB conversion functions
def srgb_to_linear(c):
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def linear_to_srgb(c):
    if c <= 0.0031308:
        return c * 12.92
    return 1.055 * (c ** (1/2.4)) - 0.055


def rgb_to_lab(r, g, b):
    """Convert sRGB (0-1) to CIELAB (L: 0-100, a,b: ~-128 to 127). D65 illuminant."""
    # sRGB to Linear
    r_lin = srgb_to_linear(r)
    g_lin = srgb_to_linear(g)
    b_lin = srgb_to_linear(b)
    
    # Linear to XYZ (D65)
    X = 0.4124564 * r_lin + 0.3575761 * g_lin + 0.1804375 * b_lin
    Y = 0.2126729 * r_lin + 0.7151522 * g_lin + 0.0721750 * b_lin
    Z = 0.0193339 * r_lin + 0.1191920 * g_lin + 0.9503041 * b_lin
    
    # XYZ to LAB
    Xn, Yn, Zn = 95.047, 100.0, 108.883
    delta = 6/29
    
    def f(t):
        if t > delta**3:
            return t ** (1/3)
        return t / (3 * delta**2) + 4/29
    
    L = 116 * f(Y / Yn) - 16
    a = 500 * (f(X / Xn) - f(Y / Yn))
    b_val = 200 * (f(Y / Yn) - f(Z / Zn))
    
    return (L, a, b_val)


def lab_to_rgb(L, a, b_val):
    """Convert CIELAB to sRGB (0-1)."""
    Xn, Yn, Zn = 95.047, 100.0, 108.883
    delta = 6/29
    
    def inv_f(y):
        if y > delta:
            return y ** 3
        return 3 * delta**2 * (y - 4/29)
    
    L_adj = (L + 16) / 116
    X = Xn * inv_f(L_adj + a / 500)
    Y = Yn * inv_f(L_adj)
    Z = Zn * inv_f(L_adj - b_val / 200)
    
    # XYZ to Linear RGB
    r_lin =  3.2404542 * X - 1.5371385 * Y - 0.4985314 * Z
    g_lin = -0.9692660 * X + 1.8760108 * Y + 0.0415560 * Z
    b_lin =  0.0556434 * X - 0.2040259 * Y + 1.0572252 * Z
    
    r = linear_to_srgb(r_lin)
    g = linear_to_srgb(g_lin)
    b = linear_to_srgb(b_lin)
    
    return (r, g, b)


def generate_colors(
    hues=None,
    hue_start=0.0,
    hue_step=30.0,
    sat_start=70.0,
    sat_step=0.0,
    sat_steps=1,
    light_start=50.0,
    light_step=0.0,
    light_steps=1,
    perceptual_multiplier=1.0,
    use_hsl_space=False,
    normalize=False,
    normalize_target=None,
    discards=[]
):
    """
    Generate color table with optional perceptual normalization.
    
    Pipeline:
    1. Generate HSL variations
    2. If normalize: Convert to LAB, set all L* to target (mean or specified)
    3. Apply global perceptual multiplier to L*
    4. Convert to RGB and output hex
    """
    # Determine hue list
    if hues is not None:
        hue_list = [float(h) % 360 for h in hues]
    else:
        hue_list = []
        current = hue_start % 360
        if hue_step <= 0:
            raise ValueError("hue_step must be positive when using start/step mode")
        while current < 360:
            hue_list.append(current)
            current += hue_step
    
    # Determine if we need LAB processing
    needs_lab = normalize or (perceptual_multiplier != 1.0) or not use_hsl_space
    
    colors = []
    lab_colors = []  # Store as (L, a, b) tuples if processing needed
    
    for h in hue_list:
        for i in range(sat_steps):
            s = sat_start + (i * sat_step)
            s = max(0, min(100, s))
            
            for j in range(light_steps):
                # l = light_start + (j * light_step)
                l = light_start + (i * light_step)
                l = max(0, min(100, l))
                
                r, g, b = hsl_to_rgb(h, s, l)
                
                if not needs_lab:
                    colors.append(rgb_to_hex(r, g, b))
                else:
                    L, a, b_lab = rgb_to_lab(r, g, b)
                    lab_colors.append((L, a, b_lab))

    filtered_colors = []
    filtered_lab_colors = []
    i = 0
    for c in colors:
        i += 1
        if not str(i) in discards:
            filtered_colors.append(c)

    i = 0
    for c in lab_colors:
        i += 1
        if not str(i) in discards:
            filtered_lab_colors.append(c)

    colors = filtered_colors
    lab_colors = filtered_lab_colors

    if not needs_lab:
        return colors

    # Step 2: Perceptual Normalization (equalize intensity)
    if normalize:
        if normalize_target is not None:
            target_L = normalize_target
        else:
            # Calculate mean perceptual lightness
            target_L = sum(lab[0] for lab in lab_colors) / len(lab_colors)
            print(f"Target L*: {target_L}")
        
        # Clamp target to valid LAB range
        target_L = max(0, min(100, target_L))
        
        # Normalize all colors to target L, preserving hue (a, b)
        lab_colors = [(target_L, a, b) for (L, a, b) in lab_colors]
    
    # Step 3: Apply final global perceptual multiplier
    final_colors = []
    for L, a, b in lab_colors:
        L_final = L * perceptual_multiplier
        L_final = max(0, min(100, L_final))
        r, g, b = lab_to_rgb(L_final, a, b)
        final_colors.append(rgb_to_hex(r, g, b))
    
    return final_colors


def main():
    parser = argparse.ArgumentParser(
        description="Generate perceptually normalized color tables",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Pipeline: HSL Generation → [Normalize to uniform L*] → Global Multiplier → Hex

Examples:
  # 32 colors normalized to same perceptual intensity, then darkened 20%
  python3 colorgen.py --hue-start 0 --hue-step 11.25 --sat-start 75 --normalize --perceptual-multiplier 0.8
  
  # Force all colors to exactly L*=55, then scale by 1.1
  python3 colorgen.py --hues "0,90,180,270" --normalize --normalize-target 55 --perceptual-multiplier 1.1
  
  # Generate with lightness variation, normalize to mean intensity, output brightened
  python3 colorgen.py --hue-start 15 --hue-step 30 --light-start 40 --light-step 10 --light-steps 3 --normalize --perceptual-multiplier 1.2
        """
    )
    
    # Hue configuration
    hue_group = parser.add_mutually_exclusive_group()
    hue_group.add_argument("--hues", type=str, metavar="LIST",
                          help='Comma-separated hue values, e.g., "0,60,120"')
    hue_group.add_argument("--hue-start", type=float, default=0.0,
                          help="Initial hue offset in degrees")
    parser.add_argument("--hue-step", type=float, default=30.0,
                       help="Separation between hues")
    
    # Saturation/Lightness configuration
    parser.add_argument("--sat-start", type=float, default=75.0,
                       help="Initial saturation (0-100)")
    parser.add_argument("--sat-step", type=float, default=0.0,
                       help="Saturation step")
    parser.add_argument("--sat-steps", type=int, default=1,
                       help="Number of saturation variations")
    parser.add_argument("--light-start", type=float, default=50.0,
                       help="Initial lightness (0-100)")
    parser.add_argument("--light-step", type=float, default=0.0,
                       help="Lightness step")
    parser.add_argument("--light-steps", type=int, default=1,
                       help="Number of lightness variations")
    
    # Perceptual processing
    parser.add_argument("--normalize", action="store_true",
                       help="Normalize all colors to same perceptual intensity (L*) before applying multiplier")
    parser.add_argument("--normalize-target", type=float, default=None, metavar="L",
                       help="Target L* value (0-100) for normalization. Default: mean of generated colors")
    parser.add_argument("--perceptual-multiplier", type=float, default=1.0,
                       help="Final global lightness multiplier (1.0=no change)")
    parser.add_argument("--hsl-space", action="store_true",
                       help="Skip LAB conversion if not normalizing (faster, less accurate perceptually)")
    
    # Output
    parser.add_argument("--python-list", action="store_true",
                       help="Output as Python list")
    parser.add_argument("--separator", type=str, default="\n",
                       help="Separator between colors")
    parser.add_argument("--stats", action="store_true",
                       help="Print generation stats to stderr")
    hue_group.add_argument("--discard", type=str, metavar="LIST",
                          help='Discard output indexes, e.g., "20,2,33"')
    
    args = parser.parse_args()
    
    if args.hsl_space and args.normalize:
        print("Warning: --hsl-space ignored because --normalize requires LAB space", file=sys.stderr)
        args.hsl_space = False
    
    try:
        if args.discard: discards = args.discard.split(",")
        else: discards = []

        colors = generate_colors(
            hues=args.hues.split(",") if args.hues else None,
            hue_start=args.hue_start,
            hue_step=args.hue_step,
            sat_start=args.sat_start,
            sat_step=args.sat_step,
            sat_steps=args.sat_steps,
            light_start=args.light_start,
            light_step=args.light_step,
            light_steps=args.light_steps,
            perceptual_multiplier=args.perceptual_multiplier,
            use_hsl_space=args.hsl_space,
            normalize=args.normalize,
            normalize_target=args.normalize_target,
            discards=discards,
        )
        
        i = 0
        # Enable to test on light background
        # print(f"#!bg=fff\n")
        if args.python_list: print("colors = [", end="")
        for c in colors:
            i+=1
            if args.python_list:
                print(f"\"{c}\", ", end="")

            else: print(f"`FT{c}colored {i}`f")
        
        if args.python_list: print("]")

        if args.stats:
            total = len(colors)
            mode = "normalized + " if args.normalize else ""
            print(f"# Generated {total} colors ({mode}L* × {args.perceptual_multiplier})", 
                  file=sys.stderr)
                  
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()