const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { layoutPattern, stationPattern } = require("../src/engine.js");
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

test("stationPattern: one full-size tray fills row by row along its length", () => {
  const p = stationPattern({ length: 578, width: 372, depth: 170 }, { length: 178, width: 138, height: 73 },
    { rows: 2, columns: 4, layers: 2, productsPerPick: 4, squeeze: 5 });
  assert.equal(p.trays, 1);
  assert.equal(p.total, 16);
  assert.equal(p.line, "x");
  const firstLine = p.stationSlots.slice(0, 4);
  assert.ok(firstLine.every((s) => s.layer === 0 && s.y === firstLine[0].y));
  assert.deepEqual(firstLine.map((s) => s.sCol), [0, 1, 2, 3]);
});

test("stationPattern: two half trays side by side cover the 600 x 400 footprint", () => {
  const tray = { length: 378, width: 272, depth: 147 };
  const p = stationPattern(tray, { length: 178, width: 138, height: 33 },
    { rows: 2, columns: 2, layers: 3, productsPerPick: 2, squeeze: 5, traysSideBySide: 2 });
  assert.equal(p.trays, 2);
  assert.equal(p.perTray, 12);
  assert.equal(p.total, 24);
  assert.equal(p.stationCols, 4);
  assert.equal(p.stationRows, 2);
  // A line runs along the station and carries on into the second tray.
  const line = p.stationSlots.slice(0, 4);
  assert.deepEqual(line.map((s) => s.tray), [0, 0, 1, 1]);
  assert.ok(line.every((s) => s.layer === 0 && Math.abs(s.y - line[0].y) < 1e-9));
  assert.ok(line[0].x < line[1].x && line[1].x < line[2].x && line[2].x < line[3].x);
  // Turned 90 degrees: the tray's 378 runs across the station, its 272 along it.
  for (const s of p.stationSlots) {
    assert.ok(Math.abs(s.y) <= tray.length / 2);
    assert.ok(Math.abs(Math.abs(s.x) - 150) <= tray.width / 2);
  }
  assert.deepEqual(p.stationSlots.map((s) => s.index), [...Array(24).keys()]);
});

test("stationPattern: three across lifted together are set down across the tray", () => {
  const p = stationPattern({ length: 567, width: 367, depth: 193 }, { length: 268, width: 115, height: 65 },
    { rows: 3, columns: 2, layers: 2, productsPerPick: 3 });
  assert.equal(p.line, "y");
  const line = p.stationSlots.slice(0, 3);
  assert.ok(line.every((s) => s.x === line[0].x));
  assert.deepEqual(line.map((s) => s.sRow), [0, 1, 2]);
});

test("stationPattern: a line sits in the tray exactly as it stood on the belt", () => {
  // 2 x 4 of 178 x 138 only fits turned: four across the 578, their 138 side along the line.
  const p = stationPattern({ length: 578, width: 372, depth: 170 }, { length: 178, width: 138, height: 73 },
    { rows: 2, columns: 4, layers: 2, productsPerPick: 4, squeeze: 5 });
  assert.equal(p.line, "x");
  assert.equal(p.lineCount, 4);
  assert.equal(p.linePitch, 138);
  assert.equal(p.crossSize, 178);
  assert.equal(p.leading, "width");          // wide edge leading on the belt
  const line = p.stationSlots.slice(0, 4);
  for (let i = 1; i < 4; i++) assert.ok(Math.abs(line[i].x - line[i - 1].x - 138) < 1e-9);   // touching, not spread out
  assert.ok(Math.abs(line[0].x + line[3].x) < 1e-9);                                        // centred in the tray
  assert.ok(Math.abs(line[3].x) + 138 / 2 <= 578 / 2);
  // The two lines are still spread evenly across the tray.
  assert.ok(Math.abs(p.stationSlots[0].y + p.stationSlots[4].y) < 1e-9);
});

test("stationPattern: not turned means narrow edge leading; three across are lifted wide edge leading", () => {
  const notTurned = stationPattern({ length: 600, width: 400, depth: 180 }, { length: 185, width: 115, height: 85 },
    { rows: 3, columns: 3, layers: 2, productsPerPick: 3 });
  assert.deepEqual([notTurned.line, notTurned.leading, notTurned.linePitch], ["x", "length", 185]);
  const across = stationPattern({ length: 567, width: 367, depth: 193 }, { length: 268, width: 115, height: 65 },
    { rows: 3, columns: 2, layers: 2, productsPerPick: 3 });
  assert.deepEqual([across.line, across.leading, across.linePitch, across.lineCount], ["y", "width", 115, 3]);
  const col = across.stationSlots.slice(0, 3);
  assert.ok(Math.abs(col[1].y - col[0].y - 115) < 1e-9 && Math.abs(col[0].y + col[2].y) < 1e-9);
});

test("stationPattern: a rigid line cannot carry on over the wall between two half trays", () => {
  const p = stationPattern({ length: 378, width: 272, depth: 147 }, { length: 178, width: 138, height: 33 },
    { rows: 2, columns: 2, layers: 3, productsPerPick: 4, squeeze: 5, traysSideBySide: 2 });
  assert.equal(p.lineCount, 2);
  assert.equal(p.productsPerPick, 2);
  const line = p.stationSlots.slice(0, 2);
  assert.deepEqual(line.map((q) => q.tray), [0, 0]);
  assert.ok(Math.abs(line[1].x - line[0].x - p.linePitch) < 1e-9);
  assert.ok(Math.abs((line[0].x + line[1].x) / 2 + 150) < 1e-9);   // centred in the first half tray
});

test("parseSkuFile: half-size trays default to two side by side", () => {
  const csv = `${HEADER}\nH,Half,175,135,70,300,x.png,Half tray,364,264,144,2,2,2,2,auto,115,\nF,Full,178,138,73,300,x.png,Full tray,578,372,170,2,4,2,4,auto,115,\nS,Single half,175,135,70,300,x.png,Half tray,364,264,144,2,2,2,2,auto,115,1`;
  const result = parseSkuFile(csv, "skus.csv");
  assert.deepEqual(result.skus.map((s) => [s.sku, s.traysSideBySide, s.perTray, s.perStation]),
    [["H", 2, 8, 16], ["F", 1, 16, 16], ["S", 1, 8, 8]]);
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
  const csv = `${HEADER}\nT300,Small punnet,175,135,70,300,x.png,Half tray,364,264,144,2,2,2,2,auto`;
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
