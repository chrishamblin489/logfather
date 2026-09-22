"""Builds simulator/pikpak-simulator.html: one self-contained file that works offline.

Inlines three.js, the engine, the arm geometry, the SKU reader, the scene, the
Leap logo and the brand font into the template, plus the built-in example products (skus/sku_template.csv).

    python simulator/build.py

Every other CSV in skus/ (a customer's list, kept out of git) also gets its own
file, pikpak-simulator-<list name>.html, with those products and their pictures
(skus/images/) built in, and their logo beside Leap's if skus/<list name>-logo.svg exists.
"""
import base64
import csv
import io
import json
import mimetypes
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
# Who the Contact us button reaches. A customer list may bring its own
# skus/<list>-contact.json with the same keys. `endpoint` is the URL the page
# posts the message to (JSON: to, subject, message, product, tray, customer,
# sentAt) for background sending; leave it empty and Send opens the customer's
# mail program instead. `noCors` posts as text/plain without reading the reply,
# for endpoints that send no CORS headers (a Power Automate HTTP trigger).
DEFAULT_CONTACT = {"name": "Steve Newman", "role": "Leap AI", "email": "steve.newman@helloleap.ai", "phone": "",
                   "endpoint": "", "noCors": False}
FONT = HERE / "assets" / "PlusJakartaSans-latin.woff2"   # Plus Jakarta Sans (OFL), as on helloleap.ai


def with_pictures(sku_csv: Path) -> str:
    """The SKU list as CSV text, each picture file name swapped for the picture itself
    (a data: URI), so the one HTML file carries everything."""
    rows = list(csv.reader(io.StringIO(sku_csv.read_text(encoding="utf-8-sig"))))
    column = rows[0].index("image") if "image" in rows[0] else None
    for row in rows[1:]:
        if column is None or column >= len(row) or not row[column].strip():
            continue
        picture = sku_csv.parent / "images" / row[column].strip()
        if picture.exists():
            kind = mimetypes.guess_type(picture.name)[0] or "image/jpeg"
            row[column] = f"data:{kind};base64,{base64.b64encode(picture.read_bytes()).decode('ascii')}"
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows(rows)
    return out.getvalue()


def build(sku_csv: Path | None = None) -> Path:
    """The generic simulator, or with `sku_csv` a customer's own: their products in the
    dropdown from the start. Customer builds are named after the list and stay out of git."""
    page = (HERE / "pikpak-simulator.template.html").read_text(encoding="utf-8")
    for placeholder, path in PARTS.items():
        assert page.count(placeholder) == 1, placeholder
        text = with_pictures(sku_csv) if sku_csv and placeholder == "__BUILT_IN_SKUS__" else path.read_text(encoding="utf-8")
        # A literal closing script tag inside a script would end it early.
        page = page.replace(placeholder, text.replace("</script", "<\\/script"))
    logo = sku_csv.with_name(sku_csv.stem + "-logo.svg") if sku_csv else None
    assert page.count("__CUSTOMER_LOGO__") == 1
    page = page.replace("__CUSTOMER_LOGO__", f'<div class="customer-logo">{logo.read_text(encoding="utf-8").strip()}</div>' if logo and logo.exists() else "")
    contact_file = sku_csv.with_name(sku_csv.stem + "-contact.json") if sku_csv else None
    contact = json.loads(contact_file.read_text(encoding="utf-8")) if contact_file and contact_file.exists() else DEFAULT_CONTACT
    assert page.count("__CONTACT__") == 1
    page = page.replace("__CONTACT__", json.dumps(contact).replace("</", "<\\/"))
    assert page.count("__CUSTOMER_STYLE__") == 1
    page = page.replace("__CUSTOMER_STYLE__", '[data-customer-build="hide"] { display: none; }' if sku_csv else "")
    assert page.count("__FONT__") == 1
    page = page.replace("__FONT__", base64.b64encode(FONT.read_bytes()).decode("ascii"))
    out = HERE / (f"pikpak-simulator-{sku_csv.stem}.html" if sku_csv else "pikpak-simulator.html")
    out.write_text(page, encoding="utf-8", newline="\n")
    return out


if __name__ == "__main__":
    lists = [None] + sorted(p for p in (HERE / "skus").glob("*.csv") if p.name != "sku_template.csv")
    for sku_list in lists:
        built = build(sku_list)
        print(f"{built} ({built.stat().st_size / 1024:.0f} KB)")
