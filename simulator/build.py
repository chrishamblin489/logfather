"""Builds simulator/pikpak-simulator.html: one self-contained file that works offline.

Inlines three.js, the engine, the arm geometry, the SKU reader, the scene, the
Leap logo and the brand font into the template, plus the built-in example products (skus/sku_template.csv).

    python simulator/build.py
"""
import base64
from pathlib import Path

HERE = Path(__file__).resolve().parent
PARTS = {
    "__THREE__": HERE / "vendor" / "three.min.js",
    "__ENGINE__": HERE / "src" / "engine.js",
    "__AUBO__": HERE / "src" / "aubo_i10.js",
    "__SKU__": HERE / "src" / "sku.js",
    "__SCENE__": HERE / "src" / "scene.js",
    "__BUILT_IN_SKUS__": HERE / "skus" / "sku_template.csv",
    "__LOGO__": HERE / "assets" / "leap-logo.svg",
}
FONT = HERE / "assets" / "PlusJakartaSans-latin.woff2"   # Plus Jakarta Sans (OFL), as on helloleap.ai


def build() -> Path:
    page = (HERE / "pikpak-simulator.template.html").read_text(encoding="utf-8")
    for placeholder, path in PARTS.items():
        assert page.count(placeholder) == 1, placeholder
        text = path.read_text(encoding="utf-8")
        # A literal closing script tag inside a script would end it early.
        page = page.replace(placeholder, text.replace("</script", "<\\/script"))
    assert page.count("__FONT__") == 1
    page = page.replace("__FONT__", base64.b64encode(FONT.read_bytes()).decode("ascii"))
    out = HERE / "pikpak-simulator.html"
    out.write_text(page, encoding="utf-8", newline="\n")
    return out


if __name__ == "__main__":
    built = build()
    print(f"{built} ({built.stat().st_size / 1024:.0f} KB)")
