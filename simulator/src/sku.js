// SKU files for the PikPak simulator: read what the sales team uploads (CSV
// saved from Excel, or JSON), check it, and hand the simulation what it needs.
// One row = one product in one tray: product size and weight, a picture for
// its top face, the tray, the layer layout (rows x columns) and the layers.
// Pure logic, no DOM. Units: millimetres and grams.
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory(require("./engine.js"));
  else root.PikPakSku = factory(root.PikPakEngine);
})(typeof self !== "undefined" ? self : this, function (engine) {
  "use strict";

  // Canonical field -> the header spellings accepted (compared lower-case
  // with everything but letters and digits removed).
  const FIELDS = {
    sku: ["sku", "skucode", "code", "productcode"],
    name: ["name", "productname", "description"],
    productLength: ["productlengthmm", "productlength", "lengthmm", "length"],
    productWidth: ["productwidthmm", "productwidth", "widthmm", "width"],
    productHeight: ["productheightmm", "productheight", "heightmm", "height"],
    weightG: ["weightg", "weightgrams", "productweightg", "weight"],
    weightKg: ["weightkg", "productweightkg"],
    image: ["image", "imagefile", "picture", "photo"],
    trayName: ["trayname", "tray", "crate", "cratename"],
    trayLength: ["traylengthmm", "traylength", "cratelengthmm"],
    trayWidth: ["traywidthmm", "traywidth", "cratewidthmm"],
    trayDepth: ["traydepthmm", "traydepth", "trayheightmm", "cratedepthmm"],
    rows: ["rows", "rowsperlayer"],
    columns: ["columns", "cols", "columnsperlayer"],
    layers: ["layers", "numberoflayers", "layercount"],
    productsPerPick: ["productsperpick", "perpick", "pickgroup"],
    orientation: ["orientation"],
    traysSideBySide: ["trayssidebyside", "sidebyside", "traysperstation"],
    infeedPpm: ["infeedppm", "ppm", "packsperminute", "packsperminuteppm"],
  };
  // Tray length and width may be left blank: most trays are 600 x 400 mm.
  const DEFAULT_TRAY = { length: 600, width: 400 };
  // What one product may give in each direction before a layout is refused
  // (Chris, 2026-09-21: real punnet layouts run a few mm over on paper).
  const SQUEEZE_MM = 5;
  // Half-size trays (about 400 x 300 outside) go through in pairs, side by
  // side, covering the same footprint as one 600 x 400 tray.
  const isHalfTray = (tray) => tray.length <= 400 && tray.width <= 300;
  const REQUIRED = ["sku", "productLength", "productWidth", "productHeight",
    "trayDepth", "rows", "columns", "layers"];
  const TEMPLATE_HEADER = ["sku", "name", "product_length_mm", "product_width_mm", "product_height_mm",
    "weight_g", "image", "tray_name", "tray_length_mm", "tray_width_mm", "tray_depth_mm",
    "rows", "columns", "layers", "products_per_pick", "orientation", "infeed_ppm", "trays_side_by_side"];

  const squash = (s) => String(s == null ? "" : s).toLowerCase().replace(/[^a-z0-9]/g, "");

  // CSV as Excel writes it: quoted cells, doubled quotes, CRLF, a BOM, and a
  // semicolon instead of a comma where the comma is the decimal mark.
  function parseCsv(text) {
    const src = String(text).replace(/^\uFEFF/, "");
    const firstLine = src.split(/\r?\n/, 1)[0];
    const count = (ch) => firstLine.split(ch).length - 1;
    const delimiter = count(";") > count(",") ? ";" : count("\t") > count(",") ? "\t" : ",";
    const rows = [];
    let row = [], cell = "", quoted = false;
    for (let i = 0; i < src.length; i++) {
      const ch = src[i];
      if (quoted) {
        if (ch === '"' && src[i + 1] === '"') { cell += '"'; i++; }
        else if (ch === '"') quoted = false;
        else cell += ch;
      } else if (ch === '"') quoted = true;
      else if (ch === delimiter) { row.push(cell); cell = ""; }
      else if (ch === "\n" || ch === "\r") {
        if (ch === "\r" && src[i + 1] === "\n") i++;
        row.push(cell); rows.push(row); row = []; cell = "";
      } else cell += ch;
    }
    if (cell !== "" || row.length) { row.push(cell); rows.push(row); }
    return { delimiter, rows: rows.filter((r) => r.some((c) => c.trim() !== "")) };
  }

  function toNumber(value, decimalComma) {
    if (typeof value === "number") return value;
    let s = String(value == null ? "" : value).trim().replace(/\s|mm$|g$|kg$/gi, "");
    if (decimalComma || /^\d+,\d+$/.test(s)) s = s.replace(",", ".");
    return s === "" ? NaN : Number(s);
  }

  // Raw records ({header: value}) -> checked SKUs plus a list of problems.
  // A record with an error is left out; a warning keeps it in.
  function buildSkus(records, options) {
    const opts = options || {};
    const skus = [], problems = [];
    records.forEach((record, index) => {
      const line = (opts.firstLine || 1) + index;
      const raw = {};
      for (const key of Object.keys(record)) {
        const field = Object.keys(FIELDS).find((f) => FIELDS[f].includes(squash(key)) || squash(f) === squash(key));
        if (field && raw[field] === undefined) raw[field] = record[key];
      }
      const blank = (field) => raw[field] === undefined || String(raw[field]).trim() === "";
      if (blank("trayLength")) raw.trayLength = DEFAULT_TRAY.length;
      if (blank("trayWidth")) raw.trayWidth = DEFAULT_TRAY.width;
      const num = (field) => toNumber(raw[field], opts.decimalComma);
      const label = String(raw.sku || "").trim() || `line ${line}`;
      const errors = [];
      for (const field of REQUIRED) {
        if (raw[field] === undefined || String(raw[field]).trim() === "") errors.push(`${field} is missing`);
      }
      const sizes = ["productLength", "productWidth", "productHeight", "trayLength", "trayWidth", "trayDepth"];
      for (const field of sizes) {
        if (raw[field] !== undefined && String(raw[field]).trim() !== "" && !(num(field) > 0)) errors.push(`${field} must be a number above 0 (mm)`);
      }
      for (const field of ["rows", "columns", "layers"]) {
        const v = num(field);
        if (raw[field] !== undefined && String(raw[field]).trim() !== "" && !(Number.isInteger(v) && v > 0)) errors.push(`${field} must be a whole number above 0`);
      }
      if (errors.length) {
        errors.forEach((message) => problems.push({ level: "error", line, sku: label, message }));
        return;
      }
      let weightG = num("weightG");
      if (!(weightG > 0) && num("weightKg") > 0) weightG = num("weightKg") * 1000;
      const orientation = ["along", "across"].includes(squash(raw.orientation)) ? squash(raw.orientation) : "auto";
      const perPick = num("productsPerPick");
      const sku = {
        id: `${label}@${String(raw.trayName || "").trim() || `${num("trayLength")}x${num("trayWidth")}x${num("trayDepth")}`}`,
        sku: label,
        name: String(raw.name || "").trim() || label,
        product: { length: num("productLength"), width: num("productWidth"), height: num("productHeight") },
        weightG: weightG > 0 ? weightG : null,
        image: String(raw.image || "").trim() || null,
        tray: {
          name: String(raw.trayName || "").trim() || null,
          length: num("trayLength"), width: num("trayWidth"), depth: num("trayDepth"),
        },
        rows: num("rows"), columns: num("columns"), layers: num("layers"),
        orientation,
        // The line lifted off the belt is set down as one row of the tray.
        productsPerPick: Number.isInteger(perPick) && perPick > 0 ? perPick : num("columns"),
        infeedPpm: num("infeedPpm") > 0 ? num("infeedPpm") : null,   // the customer's line rate
      };
      const warn = (message) => problems.push({ level: "warning", line, sku: label, message });
      if (sku.weightG == null) warn("no weight: the payload check is skipped");
      if (!sku.image) warn("no image: the product is drawn plain");
      // The layout has to fit: room left along the tray (x), across it (y)
      // and under the rim (z), plus the squeeze allowance, never below 0.
      const squeeze = opts.squeeze != null ? opts.squeeze : SQUEEZE_MM;
      const fit = engine.layoutPattern(sku.tray, sku.product, Object.assign({}, sku, { squeeze }));
      const t = sku.tray;
      const turned = fit.rotated ? " turned 90 degrees" : "";
      const what = {
        x: `${sku.columns} columns${turned} against the ${t.length} mm tray length (x)`,
        y: `${sku.rows} rows${turned} against the ${t.width} mm tray width (y)`,
        z: `${sku.layers} layers of ${sku.product.height} mm against the ${t.depth} mm tray depth (z)`,
      };
      if (!fit.fits) {
        for (const k of ["x", "y", "z"]) {
          if (fit.spare[k] + fit.allowance[k] < 0) {
            problems.push({ level: "error", line, sku: label,
              message: `${what[k]}: ${-fit.spare[k]} mm over, more than the ${fit.allowance[k]} mm squeeze allowance (${squeeze} mm per product)` });
          }
        }
        return;
      }
      for (const k of ["x", "y", "z"]) {
        if (fit.spare[k] < 0) warn(`tight fit, ${what[k]}: ${-fit.spare[k]} mm over, inside the ${fit.allowance[k]} mm squeeze allowance`);
      }
      sku.tight = fit.tight;
      const sideBySide = num("traysSideBySide");
      sku.traysSideBySide = sideBySide === 1 || sideBySide === 2 ? sideBySide : isHalfTray(sku.tray) ? 2 : 1;
      sku.spare = fit.spare;
      sku.rotated = fit.rotated;
      sku.perTray = fit.total;
      sku.perStation = fit.total * sku.traysSideBySide;   // packed between two tray changes
      if (skus.some((other) => other.id === sku.id)) warn("same SKU and tray as an earlier line: the later one is used");
      const at = skus.findIndex((other) => other.id === sku.id);
      if (at >= 0) skus[at] = sku; else skus.push(sku);
    });
    return { skus, problems };
  }

  // One uploaded file's text -> { skus, problems }. JSON may be an array of
  // records or { "skus": [...] }, with the same field names as the CSV.
  function parseSkuFile(text, fileName) {
    const name = String(fileName || "");
    const trimmed = String(text).replace(/^\uFEFF/, "").trim();
    if (/\.json$/i.test(name) || trimmed[0] === "[" || trimmed[0] === "{") {
      let data;
      try { data = JSON.parse(trimmed); }
      catch (error) { return { skus: [], problems: [{ level: "error", line: 0, sku: name, message: `not valid JSON: ${error.message}` }] }; }
      const records = Array.isArray(data) ? data : data && Array.isArray(data.skus) ? data.skus : null;
      if (!records) return { skus: [], problems: [{ level: "error", line: 0, sku: name, message: "JSON must be a list of SKUs or { \"skus\": [...] }" }] };
      return buildSkus(records);
    }
    const { delimiter, rows } = parseCsv(trimmed);
    if (rows.length < 2) return { skus: [], problems: [{ level: "error", line: 1, sku: name, message: "no SKU lines under the header" }] };
    const header = rows[0].map((h) => h.trim());
    const records = rows.slice(1).map((cells) => {
      const record = {};
      header.forEach((h, i) => { record[h] = (cells[i] || "").trim(); });
      return record;
    });
    return buildSkus(records, { firstLine: 2, decimalComma: delimiter === ";" });
  }

  // Pair each SKU's image cell with an uploaded picture by file name (folder
  // and case ignored; the extension may be left off). URLs and data: URIs
  // need no file. Returns { matches: {skuId: fileName}, missing: [skuId] }.
  function matchImages(skus, fileNames) {
    const base = (s) => String(s).split(/[\\/]/).pop().toLowerCase();
    const stem = (s) => base(s).replace(/\.[a-z0-9]+$/, "");
    const matches = {}, missing = [];
    for (const sku of skus) {
      if (!sku.image || /^(https?:|data:)/i.test(sku.image)) continue;
      const hit = fileNames.find((f) => base(f) === base(sku.image)) || fileNames.find((f) => stem(f) === stem(sku.image));
      if (hit) matches[sku.id] = hit; else missing.push(sku.id);
    }
    return { matches, missing };
  }

  return { parseCsv, parseSkuFile, buildSkus, matchImages, TEMPLATE_HEADER, DEFAULT_TRAY, SQUEEZE_MM };
});
