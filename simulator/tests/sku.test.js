const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { layoutPattern } = require("../src/engine.js");
const { parseCsv, parseSkuFile, matchImages, TEMPLATE_HEADER } = require("../src/sku.js");

const HEADER = TEMPLATE_HEADER.join(",");
const errors = (result) => result.problems.filter((p) => p.level === "error");

test("layoutPattern: spare room in x, y and z", () => {
  const fit = layoutPattern({ length: 600, width: 400, depth: 180 }, { length: 185, width: 115, height: 85 },
    { rows: 3, columns: 3, layers: 2 });
  assert.deepEqual(fit.spare, { x: 45, y: 55, z: 10 });
  assert.equal(fit.fits, true);
  assert.equal(fit.total, 18);
  for (const slot of fit.slots) {
    assert.ok(Math.abs(slot.x) + 185 / 2 <= 300 + 1e-9);
    assert.ok(Math.abs(slot.y) + 115 / 2 <= 200 + 1e-9);
    assert.ok(slot.z + 85 <= 180);
  }
});

test("layoutPattern: an exact fit has 0 spare and still fits", () => {
  const fit = layoutPattern({ length: 600, width: 400, depth: 100 }, { length: 200, width: 200, height: 50 },
    { rows: 2, columns: 3, layers: 2 });
  assert.deepEqual(fit.spare, { x: 0, y: 0, z: 0 });
  assert.equal(fit.fits, true);
});

test("layoutPattern: each direction fails on its own", () => {
  const tray = { length: 600, width: 400, depth: 180 };
  const product = { length: 185, width: 115, height: 85 };
  const tooLong = layoutPattern(tray, product, { rows: 3, columns: 4, layers: 2, orientation: "along" });
  assert.equal(tooLong.spare.x, -140);
  assert.equal(tooLong.fits, false);
  const tooWide = layoutPattern(tray, product, { rows: 4, columns: 3, layers: 2, orientation: "along" });
  assert.equal(tooWide.spare.y, -60);
  assert.equal(tooWide.fits, false);
  const tooTall = layoutPattern(tray, product, { rows: 3, columns: 3, layers: 3 });
  assert.equal(tooTall.spare.z, -75);
  assert.equal(tooTall.fits, false);
});

test("layoutPattern: auto turns the product only when that is what fits", () => {
  const tray = { length: 600, width: 400, depth: 100 };
  const product = { length: 190, width: 120, height: 90 };
  assert.equal(layoutPattern(tray, product, { rows: 3, columns: 3, layers: 1 }).rotated, false);
  const turned = layoutPattern(tray, product, { rows: 2, columns: 5, layers: 1 });
  assert.equal(turned.rotated, true);
  assert.deepEqual(turned.spare, { x: 0, y: 20, z: 10 });
  assert.equal(turned.fits, true);
});

test("parseCsv: quotes, doubled quotes, CRLF, BOM and blank lines", () => {
  const { rows } = parseCsv('﻿a,b\r\n"x, y","say ""hi"""\r\n\r\n1,2\r\n');
  assert.deepEqual(rows, [["a", "b"], ["x, y", 'say "hi"'], ["1", "2"]]);
});

test("parseSkuFile: the shipped template loads clean", () => {
  const file = path.join(__dirname, "..", "skus", "sku_template.csv");
  const result = parseSkuFile(fs.readFileSync(file, "utf8"), "sku_template.csv");
  assert.deepEqual(errors(result), []);
  assert.equal(result.skus.length, 2);
  const [first, second] = result.skus;
  assert.deepEqual(first.product, { length: 185, width: 115, height: 85 });
  assert.equal(first.weightG, 265);
  assert.equal(first.perTray, 18);
  assert.equal(first.infeedPpm, 60);
  assert.equal(second.infeedPpm, null);
  assert.deepEqual(first.spare, { x: 45, y: 55, z: 10 });
  // Tray length and width left blank: the usual 600 x 400.
  assert.deepEqual(second.tray, { name: null, length: 600, width: 400, depth: 150 });
  assert.equal(second.productsPerPick, 4);   // defaults to the columns
});

test("parseSkuFile: a layout that does not fit is rejected, with the shortfall in mm", () => {
  const csv = `${HEADER}\nBIG,Too long,185,115,85,265,,T,600,400,180,3,4,2,,along\nTALL,Too tall,185,115,85,265,,T,600,400,180,3,3,3,,\nOK,Fine,185,115,85,265,,T,600,400,180,3,3,2,,`;
  const result = parseSkuFile(csv, "skus.csv");
  assert.deepEqual(result.skus.map((s) => s.sku), ["OK"]);
  const messages = errors(result).map((p) => `${p.sku}: ${p.message}`);
  assert.equal(messages.length, 2);
  assert.match(messages[0], /^BIG: 4 columns against the 600 mm tray length \(x\): 140 mm over, more than the 20 mm squeeze allowance/);
  assert.match(messages[1], /^TALL: 3 layers of 85 mm against the 180 mm tray depth \(z\): 75 mm over, more than the 15 mm/);
  assert.equal(errors(result)[0].line, 2);
});

test("layoutPattern: squeeze allowance is per product, per direction", () => {
  const tray = { length: 364, width: 264, depth: 144 };
  const product = { length: 175, width: 135, height: 70 };
  const strict = layoutPattern(tray, product, { rows: 2, columns: 2, layers: 2 });
  assert.equal(strict.spare.y, -6);
  assert.equal(strict.fits, false);
  const squeezed = layoutPattern(tray, product, { rows: 2, columns: 2, layers: 2, squeeze: 5 });
  assert.deepEqual(squeezed.allowance, { x: 10, y: 10, z: 10 });
  assert.equal(squeezed.fits, true);
  assert.equal(squeezed.tight, true);
  assert.equal(squeezed.slotSize.width, 132); // drawn squeezed, inside the tray
  for (const slot of squeezed.slots) assert.ok(Math.abs(slot.y) + squeezed.slotSize.width / 2 <= 132 + 1e-9);
  // 11 mm over with 2 rows is past the 10 mm allowance.
  assert.equal(layoutPattern({ ...tray, width: 259 }, product, { rows: 2, columns: 2, layers: 2, squeeze: 5 }).fits, false);
  // Plenty of room: not tight.
  assert.equal(layoutPattern({ length: 600, width: 400, depth: 200 }, product, { rows: 2, columns: 2, layers: 2, squeeze: 5 }).tight, false);
});

test("parseSkuFile: a layout inside the 5 mm squeeze allowance loads with a tight-fit warning", () => {
  const csv = `${HEADER}\nT300,Tesco 300g,175,135,70,300,x.png,TESCO Half,364,264,144,2,2,2,2,auto`;
  const result = parseSkuFile(csv, "skus.csv");
  assert.deepEqual(errors(result), []);
  assert.equal(result.skus[0].tight, true);
  assert.match(result.problems[0].message, /^tight fit, 2 rows against the 264 mm tray width \(y\): 6 mm over, inside the 10 mm/);
});

test("parseSkuFile: missing and bad values are errors, missing weight and image only warn", () => {
  const csv = `${HEADER}\nA,,185,,85,,,T,600,400,180,3,3,2,,\nB,,185,115,85,,,T,600,400,180,three,3,2,,\nC,,185,115,85,,,T,600,400,180,3,3,2,,`;
  const result = parseSkuFile(csv, "skus.csv");
  assert.deepEqual(result.skus.map((s) => s.sku), ["C"]);
  assert.ok(errors(result).some((p) => p.sku === "A" && /productWidth is missing/.test(p.message)));
  assert.ok(errors(result).some((p) => p.sku === "B" && /rows must be a whole number/.test(p.message)));
  const warnings = result.problems.filter((p) => p.level === "warning" && p.sku === "C").map((p) => p.message);
  assert.equal(warnings.length, 2);
});

test("parseSkuFile: semicolons with decimal commas, loose headers, kg", () => {
  const csv = "SKU;Product Length (mm);Product Width (mm);Product Height (mm);Weight kg;Tray depth mm;Rows;Cols;Layers\nX1;185,5;115;85;0,265;180;3;3;2";
  const result = parseSkuFile(csv, "export.csv");
  assert.deepEqual(errors(result), []);
  assert.equal(result.skus[0].product.length, 185.5);
  assert.equal(result.skus[0].weightG, 265);
});

test("parseSkuFile: JSON with the same fields", () => {
  const json = JSON.stringify({ skus: [{ sku: "J1", product_length_mm: 140, product_width_mm: 140, product_height_mm: 70, weight_g: 420, tray_depth_mm: 150, rows: 2, columns: 4, layers: 2 }] });
  const result = parseSkuFile(json, "skus.json");
  assert.deepEqual(errors(result), []);
  assert.equal(result.skus[0].perTray, 16);
  assert.equal(errors(parseSkuFile("{not json", "skus.json")).length, 1);
});

test("matchImages: by file name, ignoring folder, case and a missing extension", () => {
  const skus = [
    { id: "a", image: "Punnet.JPG" }, { id: "b", image: "images/tub" },
    { id: "c", image: "https://example.com/x.png" }, { id: "d", image: "nowhere.png" }, { id: "e", image: null },
  ];
  const result = matchImages(skus, ["punnet.jpg", "tub.png"]);
  assert.deepEqual(result.matches, { a: "punnet.jpg", b: "tub.png" });
  assert.deepEqual(result.missing, ["d"]);
});
