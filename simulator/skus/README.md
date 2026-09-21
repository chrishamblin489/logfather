# SKU files for the simulator

Fill in `sku_template.csv` in Excel (keep it as CSV), one line per product in
one tray. The same product in two trays is two lines. In the simulator, upload
the CSV together with the product pictures in one go.

| Column | Needed | What it is |
|---|---|---|
| `sku` | yes | Your product code |
| `name` | no | Shown in the product list |
| `product_length_mm`, `product_width_mm`, `product_height_mm` | yes | Outside size of one product, mm |
| `weight_g` | recommended | One product, grams (`weight_kg` also works). Used for the arm's 10 kg payload check |
| `image` | recommended | File name of a picture of the product **from above**, uploaded with the CSV. It is put on the product's top face |
| `tray_name` | no | e.g. `Standard 600x400` |
| `tray_length_mm`, `tray_width_mm` | no | **Inside** size. Blank = 600 x 400 |
| `tray_depth_mm` | yes | Inside depth, floor to rim |
| `rows`, `columns` | yes | Products per layer. Columns run along the tray length |
| `layers` | yes | Layers in a full tray |
| `products_per_pick` | no | Products lifted together. Blank = one row (`columns`) |
| `orientation` | no | `along`, `across` (product turned 90 degrees) or `auto` |
| `infeed_ppm` | no | The customer's line rate for this product, packs per minute. Sets the infeed slider when the SKU is chosen |

## The fit check

Every line is checked before it reaches the simulation. The room left over is
worked out in three directions and none may be below 0:

- x: tray length - columns x product length
- y: tray width - rows x product width
- z: tray depth - layers x product height

(with the product turned 90 degrees, length and width swap). Real punnets give
a little (rims flex, sides taper, film lids settle), so each product is allowed
to squeeze by up to 5 mm in each direction: 4 columns may run up to 20 mm over
in x. A layout inside that allowance loads with a "tight fit" note; one beyond
it is refused, and the message says which direction is over and by how many mm.
Use the tray's inside size: a nominal 600 x 400 tray is smaller inside.

Customer SKU files (any `.csv` here except the template) are not committed to git.

## Pictures

A straight-down photo on a plain background works best. If a picture is
missing or poor, ask for a better one to be found and added to the library.
